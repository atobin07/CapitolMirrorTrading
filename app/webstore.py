"""Stupid-simple web storefront: land → tap → Apple Pay → PDF, no login.

Same engine (persona, catalog, PDF products) with a mobile-first chat + one-tap
Stripe checkout. On its own domain there's no platform to ban you, and web gives
you Stripe *webhooks* → payment is confirmed instantly and delivery is automatic.

Flow:
  GET  /                     storefront page (products + chat)
  POST /api/chat             talk to the engine
  POST /api/checkout         create a Stripe Checkout Session → returns pay URL
  POST /api/stripe/webhook   Stripe confirms payment → mark paid + issue download
  GET  /success?session=..   thank-you page with the download link
  GET  /api/order?session=.. poll order state (webhook can lag the redirect)
  GET  /d/<token>            serve the purchased PDF (unguessable link)

Run:  python run.py web
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from decimal import Decimal

from app import products as pdl
from app.sales import format_price

log = logging.getLogger(__name__)


# ── pure helpers (server-independent → unit-testable) ────────────────────────
def verify_stripe_signature(secret: str, raw_body: bytes, header: str | None) -> bool:
    """Validate Stripe's `Stripe-Signature: t=..,v1=..` header."""
    if not secret:
        return True  # dev only: no secret configured
    if not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        return False
    signed = f"{t}.{raw_body.decode()}".encode()
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def fulfil_web_order(store, session_obj: dict) -> str | None:
    """Given a completed Stripe session, mark the order paid and return a
    download token (idempotent). None if it can't be fulfilled."""
    pid = session_obj.get("id")
    if not pid:
        return None
    order = store.order_by_payment_id("stripe", pid)
    if not order:
        return None
    if order["state"] == "paid":
        return store.download_for_order(order["id"])

    total = session_obj.get("amount_total")
    if total is not None and Decimal(total) / 100 < Decimal(str(order["amount"])):
        store.set_order_state(order["id"], "failed", reason="amount mismatch")
        return None

    ref = session_obj.get("payment_intent") or pid
    if not store.consume_payment("stripe", ref, order["id"]):
        return store.download_for_order(order["id"])  # already handled
    store.set_order_state(order["id"], "paid", reason="web stripe")
    return store.issue_download(order["id"])


def process_event(store, payload: dict) -> str | None:
    if payload.get("type") == "checkout.session.completed":
        return fulfil_web_order(store, payload.get("data", {}).get("object", {}))
    return None


def _product_by_id(cfg, product_id):
    for p in cfg.catalog.get("products", []):
        if str(p.get("id")) == str(product_id):
            return p
    return None


def _session_key(raw: str) -> int:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else (abs(hash(raw)) % (10 ** 12))


# ── server ───────────────────────────────────────────────────────────────────
async def run_server(cfg) -> None:
    import asyncio

    from aiohttp import web

    from app.llm import build_llm
    from app.meta_webhook import handle_message
    from app.payments.stripe_gateway import StripeGateway
    from app.ratelimit import RateLimiter
    from app.sales import build_system_prompt
    from app.store import Store

    store = Store(cfg.database_path)
    llm = build_llm(cfg)
    rate_limiter = RateLimiter(cfg.rate_limit_per_min)
    system_prompt = build_system_prompt(
        cfg.business_name, cfg.catalog, cfg.checkout_url,
        payment_methods=["card or Apple Pay"], persona_name=cfg.persona_name,
        persona_style=cfg.persona_style,
    )
    gateway = StripeGateway(
        cfg.stripe_secret_key,
        success_url=f"{cfg.store_public_url}/success?session={{CHECKOUT_SESSION_ID}}",
        cancel_url=cfg.store_public_url,
        payment_methods=cfg.stripe_payment_methods,
    )
    currency = cfg.catalog.get("currency", "USD")

    async def index(request):
        return web.Response(text=_PAGE.replace("{{BIZ}}", cfg.business_name),
                            content_type="text/html")

    async def api_products(request):
        items = [{
            "id": str(p["id"]), "title": pdl.product_title(p),
            "price": format_price(p.get("price"), currency),
            "organization": pdl.organization(p), "summary": p.get("summary", ""),
        } for p in cfg.catalog.get("products", [])]
        return web.json_response(items)

    async def api_chat(request):
        data = await request.json()
        key = str(_session_key(data.get("session", "")))
        replies = await handle_message(
            cfg, store, llm, system_prompt, rate_limiter, key,
            str(data.get("message", "")))
        return web.json_response({"replies": replies})

    async def api_checkout(request):
        data = await request.json()
        product = _product_by_id(cfg, data.get("product_id"))
        if not product:
            return web.json_response({"error": "no such product"}, status=404)
        if pdl.is_file_product(product) and not pdl.file_exists(product, cfg.catalog_dir):
            return web.json_response({"error": "temporarily unavailable"}, status=409)
        key = _session_key(data.get("session", ""))
        order_id = store.create_order(
            key, "web", "", str(product["id"]), pdl.product_title(product),
            str(product.get("price")), currency)
        try:
            link = await gateway.create_checkout(
                product_name=pdl.product_title(product),
                amount=Decimal(str(product.get("price"))), currency=currency,
                order_id=order_id, chat_id=key)
        except Exception as exc:  # noqa: BLE001
            log.error("checkout create failed: %s", exc)
            return web.json_response({"error": "checkout unavailable"}, status=502)
        store.set_order_state(order_id, "awaiting_payment", provider="stripe",
                              payment_id=link.ref)
        return web.json_response({"url": link.url})

    async def stripe_webhook(request):
        raw = await request.read()
        if not verify_stripe_signature(cfg.stripe_webhook_secret, raw,
                                       request.headers.get("Stripe-Signature")):
            return web.Response(status=400, text="bad signature")
        import json
        process_event(store, json.loads(raw.decode()))
        return web.Response(text="ok")

    def _download_url(order):
        token = store.download_for_order(order["id"])
        return f"{cfg.store_public_url}/d/{token}" if token else None

    async def api_order(request):
        order = store.order_by_payment_id("stripe", request.query.get("session", ""))
        if not order:
            return web.json_response({"state": "unknown"})
        paid = order["state"] == "paid"
        return web.json_response({
            "state": order["state"],
            "download": _download_url(order) if paid else None,
        })

    async def success(request):
        order = store.order_by_payment_id("stripe", request.query.get("session", ""))
        dl = _download_url(order) if order and order["state"] == "paid" else None
        return web.Response(
            text=_SUCCESS.replace("{{BIZ}}", cfg.business_name)
                         .replace("{{DL}}", dl or ""),
            content_type="text/html")

    async def download(request):
        order = store.resolve_download(request.match_info["token"])
        if not order or order["state"] != "paid":
            return web.Response(status=404, text="Not found")
        product = _product_by_id(cfg, order["product_id"]) or {}
        path = pdl.resolve_path(product, cfg.catalog_dir)
        if not path or not os.path.isfile(path):
            return web.Response(status=404, text="File unavailable")
        return web.FileResponse(path, headers={
            "Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/products", api_products)
    app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/checkout", api_checkout)
    app.router.add_post("/api/stripe/webhook", stripe_webhook)
    app.router.add_get("/api/order", api_order)
    app.router.add_get("/success", success)
    app.router.add_get("/d/{token}", download)

    log.info("Web storefront on %s:%s (public: %s)",
             cfg.webhook_host, cfg.webhook_port, cfg.store_public_url)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, cfg.webhook_host, cfg.webhook_port).start()
    while True:
        await asyncio.sleep(3600)


_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{{BIZ}}</title><style>
:root{--bg:#f4f6f9;--card:#fff;--ink:#111827;--soft:#6b7280;--line:#e5e7eb;--accent:#111827;--go:#12805c}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:520px;margin:0 auto;padding:20px 16px 40px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
h1{font-size:20px;margin:0;letter-spacing:-.01em}.lock{font-size:12px;color:var(--soft)}
.sub{color:var(--soft);font-size:13.5px;margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:10px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card h3{margin:0 0 2px;font-size:15.5px}.card .org{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--soft)}
.card p{margin:4px 0 12px;color:var(--soft);font-size:13px}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px}
.price{font-weight:700;font-variant-numeric:tabular-nums}
button{font-family:inherit;font-weight:600;border:0;border-radius:10px;cursor:pointer}
.buy{background:var(--accent);color:#fff;padding:10px 16px;font-size:14px}
.buy:active{transform:translateY(1px)}.buy:disabled{opacity:.5}
.chat{margin-top:22px}.chat h2{font-size:13px;text-transform:uppercase;letter-spacing:.1em;color:var(--soft);margin:0 0 8px}
#log{display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
.b{max-width:85%;padding:8px 11px;border-radius:14px;font-size:14px;line-height:1.4}
.b.you{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:5px}
.b.bot{align-self:flex-start;background:#fff;border:1px solid var(--line);border-bottom-left-radius:5px}
.inbar{display:flex;gap:8px}#msg{flex:1;padding:11px 13px;border:1px solid var(--line);border-radius:10px;font-size:15px}
.send{background:var(--accent);color:#fff;padding:0 16px}
footer{margin-top:26px;text-align:center;color:var(--soft);font-size:12px}
.trust{display:flex;gap:6px;align-items:center;justify-content:center;margin-top:6px}
</style></head><body><div class=wrap>
<header><h1>{{BIZ}}</h1><span class=lock>🔒 Secure checkout</span></header>
<p class=sub>Instant delivery. Pay with Apple Pay, Google Pay, or card — no account needed.</p>
<div id=products></div>
<div class=chat><h2>Questions? Ask away</h2><div id=log></div>
<div class=inbar><input id=msg placeholder="type a message…" autocomplete=off>
<button class=send onclick=send()>Send</button></div></div>
<footer>Payments secured by Stripe · files delivered instantly<div class=trust>🔒 SSL encrypted</div></footer>
</div><script>
const S=localStorage.getItem('s')||(localStorage.setItem('s',Math.random().toString(36).slice(2)),localStorage.getItem('s'));
fetch('/api/products').then(r=>r.json()).then(ps=>{
 document.getElementById('products').innerHTML=ps.map(p=>`<div class=card>
 ${p.organization?`<div class=org>${p.organization}</div>`:''}
 <h3>${p.title}</h3><p>${p.summary||''}</p>
 <div class=row><span class=price>${p.price}</span>
 <button class=buy onclick="buy('${p.id}',this)">Get it</button></div></div>`).join('')
 ||'<div class=card>No products yet.</div>';});
async function buy(id,btn){btn.disabled=1;btn.textContent='…';
 const r=await fetch('/api/checkout',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({product_id:id,session:S})}).then(r=>r.json());
 if(r.url)location.href=r.url;else{btn.disabled=0;btn.textContent='Get it';alert(r.error||'Try again')}}
function add(t,who){const d=document.createElement('div');d.className='b '+who;d.textContent=t;
 document.getElementById('log').appendChild(d);d.scrollIntoView()}
async function send(){const i=document.getElementById('msg');const t=i.value.trim();if(!t)return;i.value='';add(t,'you');
 const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({message:t,session:S})}).then(r=>r.json());
 (r.replies||[]).forEach(x=>add(x,'bot'))}
document.getElementById('msg').addEventListener('keydown',e=>{if(e.key==='Enter')send()});
</script></body></html>"""


_SUCCESS = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Thank you</title><style>
body{margin:0;font-family:-apple-system,system-ui,sans-serif;background:#f4f6f9;color:#111827;text-align:center}
.wrap{max-width:460px;margin:0 auto;padding:60px 20px}.tick{font-size:44px}
h1{font-size:22px;margin:10px 0 6px}p{color:#6b7280}
a.dl{display:inline-block;margin-top:18px;background:#12805c;color:#fff;text-decoration:none;
padding:14px 22px;border-radius:12px;font-weight:700}
</style></head><body><div class=wrap><div class=tick>✅</div>
<h1>Payment confirmed</h1><p>Thanks for buying from {{BIZ}}.</p>
<div id=box><p>Preparing your download…</p></div>
<script>
const DL="{{DL}}";const p=new URLSearchParams(location.search);
function show(u){document.getElementById('box').innerHTML='<a class=dl href="'+u+'">⬇ Download your files</a>'
 +'<p style=margin-top:14px;font-size:13px>A copy is on its way to your email too.</p>'}
if(DL)show(DL);else{let n=0;const t=setInterval(async()=>{n++;
 const r=await fetch('/api/order?session='+p.get('session')).then(r=>r.json());
 if(r.download){clearInterval(t);show(r.download)}else if(n>20){clearInterval(t);
  document.getElementById('box').innerHTML='<p>Almost there — refresh in a moment, or check your email.</p>'}},1500)}
</script></body></html>"""
