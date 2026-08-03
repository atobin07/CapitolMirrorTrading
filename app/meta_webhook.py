"""Instagram / Messenger DM front-end for the same sales engine.

Meta *pushes* DMs to a public HTTPS webhook (unlike Telegram's polling), so this
runs a small web server. Incoming DMs go through the same LLM persona, history,
disclosure, and rate limiting as the Telegram bot; replies are sent back via the
Graph API. Selling on IG hands off to a web checkout link (IG has no native
in-DM payment), so pair this with the web storefront.

Setup (all on Meta's side) is documented in the README. Requires: IG Pro account
+ linked FB Page + a Meta app with Instagram messaging permissions + App Review.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from app.humanize import strip_markdown

log = logging.getLogger(__name__)


# ── pure helpers (no server dependency → unit-testable) ──────────────────────
def verify_challenge(params: dict, verify_token: str) -> str | None:
    """Meta's GET verification handshake: echo hub.challenge if the token matches."""
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == verify_token
    ):
        return params.get("hub.challenge")
    return None


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Validate Meta's X-Hub-Signature-256 header (HMAC-SHA256 of the raw body)."""
    if not app_secret:
        return True  # no secret configured → skip (dev only)
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def parse_events(payload: dict) -> list[tuple[str, str]]:
    """Extract (sender_id, text) from an IG/Messenger webhook payload.

    Skips echoes (our own outgoing messages) and non-text events.
    """
    out: list[tuple[str, str]] = []
    for entry in payload.get("entry", []):
        for m in entry.get("messaging", []):
            msg = m.get("message") or {}
            if msg.get("is_echo"):
                continue
            text = msg.get("text")
            sender = (m.get("sender") or {}).get("id")
            if sender and text:
                out.append((str(sender), text))
    return out


class MetaMessenger:
    """Sends replies through the Graph API."""

    def __init__(self, page_token: str, graph_version: str = "v21.0", timeout: int = 20):
        self.token = page_token
        self.base = f"https://graph.facebook.com/{graph_version}"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_text(self, recipient_id: str, text: str) -> bool:
        try:
            resp = await self._client.post(
                f"{self.base}/me/messages",
                params={"access_token": self.token},
                json={
                    "recipient": {"id": recipient_id},
                    "messaging_type": "RESPONSE",
                    "message": {"text": text[:1000]},
                },
            )
            if resp.status_code >= 400:
                log.warning("Meta send %s: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except httpx.HTTPError as exc:
            log.warning("Meta send failed: %s", exc)
            return False


async def handle_message(
    cfg, store, llm, system_prompt, rate_limiter, sender_id: str, text: str
) -> list[str]:
    """Run one inbound DM through the engine; return the messages to send back."""
    key = int(sender_id) if sender_id.isdigit() else abs(hash(sender_id))
    text = text.strip()[: cfg.max_input_chars]

    if rate_limiter is not None:
        allowed, warn = rate_limiter.check(key)
        if not allowed:
            return ["getting a lot at once — give me a sec 🙏"] if warn else []

    out: list[str] = []
    # One-time disclosure (compliance), same as Telegram.
    if cfg.disclosure_enabled and store.needs_disclosure(key):
        out.append(cfg.disclosure_text.replace("{business}", cfg.business_name))
        store.mark_disclosed(key)

    history = store.get_history(key)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    try:
        reply = await llm.chat(messages)
    except Exception as exc:  # noqa: BLE001
        log.error("LLM error on IG message: %s", exc)
        return out + ["one sec, my phone's being weird — say that again?"]

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    store.save_history(key, history, f"ig:{sender_id}", "")
    out.append(strip_markdown(reply))
    return out


async def run_server(cfg) -> None:
    """Start the aiohttp webhook server (blocks)."""
    from aiohttp import web  # lazy import so the rest stays testable without aiohttp

    from app.llm import build_llm
    from app.ratelimit import RateLimiter
    from app.sales import build_system_prompt
    from app.store import Store

    store = Store(cfg.database_path)
    llm = build_llm(cfg)
    rate_limiter = RateLimiter(cfg.rate_limit_per_min)
    messenger = MetaMessenger(cfg.meta_page_token, cfg.meta_graph_version)
    system_prompt = build_system_prompt(
        cfg.business_name, cfg.catalog, cfg.checkout_url,
        payment_methods=None, persona_name=cfg.persona_name,
        persona_style=cfg.persona_style,
    )

    async def get_webhook(request):
        params = dict(request.query)
        challenge = verify_challenge(params, cfg.meta_verify_token)
        if challenge is not None:
            return web.Response(text=challenge)
        return web.Response(status=403, text="verification failed")

    async def post_webhook(request):
        raw = await request.read()
        if not verify_signature(cfg.meta_app_secret, raw,
                                request.headers.get("X-Hub-Signature-256")):
            return web.Response(status=403, text="bad signature")
        payload = await request.json()
        for sender_id, text in parse_events(payload):
            replies = await handle_message(
                cfg, store, llm, system_prompt, rate_limiter, sender_id, text
            )
            for r in replies:
                if r:
                    await messenger.send_text(sender_id, r)
        return web.Response(text="EVENT_RECEIVED")  # Meta expects a fast 200

    app = web.Application()
    app.router.add_get("/webhook", get_webhook)
    app.router.add_post("/webhook", post_webhook)
    app.router.add_get("/", lambda r: web.Response(text="ok"))

    log.info("Instagram/Messenger webhook listening on %s:%s/webhook",
             cfg.webhook_host, cfg.webhook_port)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.webhook_host, cfg.webhook_port)
    await site.start()
    import asyncio
    while True:  # serve forever
        await asyncio.sleep(3600)
