"""Preflight connection check: `python run.py --check`.

Verifies every link in the chain before you go live:
  Telegram token → Ollama reachable → model present → catalog → sellers → payments
Prints a pass/fail checklist with exactly how to fix anything broken.
"""
from __future__ import annotations

import asyncio

import httpx

from app.config import Config
from app.ollama_client import OllamaClient
from app.payments import build_verifiers

GREEN = "\033[92m"
RED = "\033[91m"
YEL = "\033[93m"
DIM = "\033[2m"
END = "\033[0m"


def _ok(label, detail=""):
    print(f"  {GREEN}✅ {label}{END}" + (f"  {DIM}{detail}{END}" if detail else ""))


def _fail(label, fix):
    print(f"  {RED}❌ {label}{END}\n     {YEL}fix:{END} {fix}")


def _warn(label, detail=""):
    print(f"  {YEL}⚠️  {label}{END}" + (f"  {DIM}{detail}{END}" if detail else ""))


async def _check_telegram(cfg: Config) -> bool:
    if not cfg.telegram_token or cfg.telegram_token.startswith("123456:ABC"):
        _fail("Telegram bot token", "set TELEGRAM_BOT_TOKEN in .env (from @BotFather)")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"https://api.telegram.org/bot{cfg.telegram_token}/getMe")
        if r.status_code == 200 and r.json().get("ok"):
            u = r.json()["result"]
            _ok("Telegram bot token", f"@{u.get('username')} ({u.get('first_name')})")
            return True
        _fail("Telegram bot token", f"Telegram rejected it ({r.status_code}). "
              "Re-copy the token from @BotFather.")
        return False
    except httpx.HTTPError as exc:
        _fail("Telegram reachability", f"can't reach api.telegram.org: {exc}")
        return False


async def _check_ollama(cfg: Config) -> bool:
    client = OllamaClient(cfg.ollama_host, cfg.ollama_model, timeout=15)
    try:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{cfg.ollama_host}/api/tags")
        except httpx.HTTPError as exc:
            _fail("Ollama reachable",
                  f"can't reach {cfg.ollama_host} ({exc}). Is Ollama running? "
                  "`ollama serve` / check OLLAMA_HOST.")
            return False
        if r.status_code != 200:
            _fail("Ollama reachable", f"{cfg.ollama_host} returned {r.status_code}")
            return False
        _ok("Ollama reachable", cfg.ollama_host)

        if await client.health():
            _ok("Model available", cfg.ollama_model)
            model_ok = True
        else:
            names = [m.get("name") for m in r.json().get("models", [])]
            _fail("Model available",
                  f"'{cfg.ollama_model}' not pulled. Run: ollama pull {cfg.ollama_model}"
                  + (f"  (have: {', '.join(names)})" if names else ""))
            model_ok = False

        # Live generation smoke test (only if the model is present).
        if model_ok:
            try:
                reply = await asyncio.wait_for(
                    client.chat([{"role": "user", "content": "Say 'ready' and nothing else."}]),
                    timeout=cfg.ollama_timeout,
                )
                _ok("Model responds", f'"{reply[:40]}"')
            except Exception as exc:  # noqa: BLE001
                _warn("Model responds", f"generation failed/slow: {exc}")
        return model_ok
    finally:
        await client.close()


def _check_catalog(cfg: Config) -> bool:
    products = cfg.catalog.get("products", [])
    if not products:
        _fail("Product catalog", "catalog.json has no products — add your real offers.")
        return False
    missing = [p.get("id", "?") for p in products
               if not p.get("name") or p.get("price") in (None, "")]
    if missing:
        _fail("Product catalog", f"products missing name/price: {missing}")
        return False
    _ok("Product catalog", f"{len(products)} products, currency {cfg.catalog.get('currency','USD')}")
    return True


def _check_sellers(cfg: Config) -> bool:
    if not cfg.seller_chat_ids:
        _warn("Seller notifications",
              "SELLER_CHAT_IDS empty — you won't get lead/order alerts. "
              "Set your numeric ID from @userinfobot.")
        return True  # non-fatal
    _ok("Seller notifications", f"{len(cfg.seller_chat_ids)} recipient(s)")
    return True


async def _check_stripe(cfg: Config) -> bool:
    from app.payments.stripe_gateway import StripeGateway
    gw = StripeGateway(cfg.stripe_secret_key)
    try:
        ok, detail = await gw.ping()
    finally:
        await gw.close()
    if ok:
        _ok("Stripe key", detail)
        if gw.live:
            pass
        else:
            _warn("Stripe mode", "TEST key — real cards won't be charged. "
                  "Use sk_live_… to take real money.")
        return True
    _fail("Stripe key", f"Stripe rejected the key ({detail}). "
          "Check STRIPE_SECRET_KEY (starts with sk_live_ or sk_test_).")
    return False


def _check_inventory(store, cfg: Config) -> None:
    products = cfg.catalog.get("products", [])
    if not products:
        return
    empties = []
    for p in products:
        n = store.available_count(str(p["id"]))
        if n <= 0:
            empties.append(p["name"])
        else:
            _ok(f"Stock: {p['name']}", f"{n} available")
    for name in empties:
        _warn(f"Stock: {name}", "0 available — load with "
              "`python run.py stock <id> <file.txt>` or it'll show as sold out.")


def _check_payments(cfg: Config) -> bool:
    if cfg.stripe_enabled:
        # Stripe check is handled separately (async) in run_checks.
        return True
    if not cfg.payments_enabled:
        _warn("Payments", "disabled (PAYMENT_PROVIDERS empty). Bot sells + hands "
              "leads to you; no in-chat checkout.")
        return True
    verifiers = build_verifiers(cfg)
    if not verifiers:
        _fail("Payments", f"PAYMENT_PROVIDERS={cfg.payment_providers} built no "
              "verifiers — check the provider names (paypal, cashapp, applepay, venmo).")
        return False
    for key, v in verifiers.items():
        mode = "human-approved (no API)" if v.manual else "API auto-verify"
        (_warn if v.manual else _ok)(f"Payment: {v.label}", mode)
    return True


async def run_checks() -> int:
    import logging
    logging.disable(logging.WARNING)  # keep the checklist clean
    cfg = Config.load()
    print(f"\n🔌 Connection check for {cfg.business_name}\n" + "─" * 44)

    print("\nTelegram")
    tg = await _check_telegram(cfg)

    print("\nBrain (Ollama)")
    ol = await _check_ollama(cfg)

    print("\nProducts")
    cat = _check_catalog(cfg)

    print("\nAlerts")
    _check_sellers(cfg)

    print("\nPayments")
    stripe_ok = True
    if cfg.stripe_enabled:
        stripe_ok = await _check_stripe(cfg)
        from app.store import Store
        store = Store(cfg.database_path)
        try:
            _check_inventory(store, cfg)
        finally:
            store.close()
    else:
        _check_payments(cfg)

    print("\n" + "─" * 44)
    essential = tg and ol and cat and stripe_ok
    if essential:
        print(f"{GREEN}Ready to sell.{END} Start it with:  python run.py\n")
        return 0
    print(f"{RED}Not ready.{END} Fix the ❌ items above, then re-run "
          f"{DIM}python run.py --check{END}\n")
    return 1


def main() -> int:
    return asyncio.run(run_checks())


if __name__ == "__main__":
    raise SystemExit(main())
