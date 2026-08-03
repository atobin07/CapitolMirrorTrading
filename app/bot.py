"""Telegram sales bot backed by Ollama."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import humanize
from app.checkout_flow import CheckoutFlow
from app.config import Config
from app.llm import backend_label, build_llm
from app.ollama_client import OllamaError
from app.ratelimit import RateLimiter
from app.payments import build_verifiers
from app.payments.paypal_gateway import PayPalGateway
from app.payments.stripe_gateway import StripeGateway
from app.payments_flow import PaymentFlow
from app.sales import build_system_prompt, detect_buying_signal
from app.store import Store

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("salesbot")

# Objects wired up in main(); handlers read them from bot_data too, but module
# globals keep the handler signatures clean.
cfg: Config
store: Store
llm: object  # OllamaClient or OpenAICompatClient (same interface)
system_prompt: str
payment_flow: PaymentFlow | None = None
checkout_flow: CheckoutFlow | None = None
rate_limiter: RateLimiter | None = None


def _display_name(update: Update) -> tuple[str, str]:
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else ""
    full_name = user.full_name if user else ""
    return username, full_name


def _disclosure_text() -> str:
    return cfg.disclosure_text.replace("{business}", cfg.business_name)


def _terms_text() -> str:
    parts = [f"📋 {cfg.business_name} — terms & refunds", ""]
    parts.append(cfg.refund_policy)
    if cfg.support_contact:
        parts.append("")
        parts.append(f"Questions? {cfg.support_contact}")
    if cfg.terms_url:
        parts.append("")
        parts.append(f"Full terms: {cfg.terms_url}")
    parts.append("")
    parts.append(
        "Payments are processed securely by Stripe. This chat is handled by an "
        "automated assistant."
    )
    return "\n".join(parts)


async def _maybe_disclose(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the one-time bot-disclosure notice (compliance)."""
    if cfg.disclosure_enabled and store.needs_disclosure(chat_id):
        await context.bot.send_message(chat_id, _disclosure_text())
        store.mark_disclosed(chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    store.reset(chat_id)
    await _maybe_disclose(chat_id, context)
    name = f" {cfg.persona_name} here." if cfg.persona_name else ""
    greeting = f"hey.{name} what's up?"
    await humanize.deliver(context.bot, chat_id, greeting, cfg)


async def terms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_terms_text())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    can_buy = cfg.stripe_enabled or cfg.payments_enabled
    buy_line = "/buy – see products and pay\n" if can_buy else ""
    await update.message.reply_text(
        "Just chat with me naturally — tell me what you need and I'll help.\n\n"
        f"{buy_line}"
        "/terms – refund policy & terms\n"
        "/start – restart our conversation\n"
        "/help – show this message"
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store.reset(update.effective_chat.id)
    await update.message.reply_text("Fresh start! What can I help you with?")


async def leads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Seller-only: list recent hot leads."""
    if update.effective_user.id not in cfg.seller_chat_ids:
        return
    rows = store.recent_leads(limit=15)
    if not rows:
        await update.message.reply_text("No leads captured yet.")
        return
    lines = ["🔥 *Recent leads:*"]
    for r in rows:
        who = r["username"] or r["full_name"] or f"id:{r['chat_id']}"
        lines.append(f"• {who}: {r['message'][:80]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _notify_sellers(context: ContextTypes.DEFAULT_TYPE, update: Update, text: str) -> None:
    username, full_name = _display_name(update)
    who = username or full_name or f"id:{update.effective_chat.id}"
    note = (
        f"🔥 *Hot lead!*\n"
        f"From: {who}\n"
        f"Said: {text[:200]}\n\n"
        f"Open the chat to close the sale."
    )
    for sid in cfg.seller_chat_ids:
        try:
            await context.bot.send_message(sid, note, parse_mode="Markdown")
        except Exception as exc:  # noqa: BLE001 - never let notify break the reply
            log.warning("Could not notify seller %s: %s", sid, exc)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()
    username, full_name = _display_name(update)

    # Cap oversized input (protects context window + compute cost).
    if len(user_text) > cfg.max_input_chars:
        user_text = user_text[: cfg.max_input_chars]

    # Rate-limit floods so one user can't choke Ollama (and the other bots).
    if rate_limiter is not None:
        allowed, should_warn = rate_limiter.check(chat_id)
        rate_limiter.cleanup()
        if not allowed:
            if should_warn:
                await update.message.reply_text(
                    "getting a lot at once — give me a sec and try again 🙏"
                )
            return

    # Compliance: disclose once, before the first real exchange.
    await _maybe_disclose(chat_id, context)

    # If the customer is mid-checkout and owes us a payment ID, that takes
    # priority over the LLM — money handling is deterministic, never AI-driven.
    if payment_flow is not None:
        if await payment_flow.maybe_handle_payment_id(update, context):
            return

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    history = store.get_history(chat_id)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        reply = await llm.chat(messages)
    except OllamaError as exc:
        log.error("Ollama error: %s", exc)
        await update.message.reply_text(
            "sorry, my phone's being weird for a sec — say that again?"
        )
        return

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})
    store.save_history(chat_id, history, username, full_name)

    await humanize.deliver(context.bot, chat_id, reply, cfg)

    # Capture + notify on strong buying intent.
    if detect_buying_signal(user_text):
        store.add_lead(chat_id, username, full_name, user_text)
        await _notify_sellers(context, update, user_text)


async def _post_init(app: Application) -> None:
    if checkout_flow is not None:
        await checkout_flow.start(app)
    healthy = await llm.health()
    if healthy:
        log.info("LLM backend ready — %s", backend_label(cfg))
    else:
        log.warning(
            "LLM backend not reachable — %s. Check your LLM/OLLAMA settings; "
            "the bot will error on every message until it responds.",
            backend_label(cfg),
        )


async def _post_shutdown(app: Application) -> None:
    await llm.close()
    if payment_flow is not None:
        await payment_flow.close()
    if checkout_flow is not None:
        await checkout_flow.close()
    store.close()


def main() -> None:
    global cfg, store, llm, system_prompt, payment_flow, checkout_flow, rate_limiter
    cfg = Config.load()
    rate_limiter = RateLimiter(cfg.rate_limit_per_min)

    problems = cfg.validate()
    for p in problems:
        log.warning("Config: %s", p)
    if not cfg.telegram_token or cfg.telegram_token.startswith("123456:ABC"):
        raise SystemExit(
            "Cannot start: TELEGRAM_BOT_TOKEN is missing. "
            "Copy .env.example to .env and fill it in."
        )

    store = Store(cfg.database_path)
    llm = build_llm(cfg)
    # Prefer autonomous hosted checkout (Stripe and/or PayPal) when configured;
    # otherwise fall back to the manual/paste-ID provider flow.
    if cfg.autonomous_checkout_enabled:
        gateways = {}
        if cfg.stripe_enabled:
            gateways["stripe"] = StripeGateway(
                cfg.stripe_secret_key,
                success_url=cfg.stripe_success_url,
                cancel_url=cfg.stripe_cancel_url,
                payment_methods=cfg.stripe_payment_methods,
            )
        if cfg.paypal_checkout_enabled:
            gateways["paypal"] = PayPalGateway(
                cfg.paypal_client_id,
                cfg.paypal_secret,
                env=cfg.paypal_env,
                return_url=cfg.paypal_return_url,
                cancel_url=cfg.paypal_cancel_url,
                brand_name=cfg.business_name,
                enable_venmo=cfg.paypal_enable_venmo,
            )
        checkout_flow = CheckoutFlow(cfg, store, gateways)
        payment_flow = None
        payment_labels = [gw.label for gw in gateways.values()]
        log.info("Autonomous checkout enabled: %s", ", ".join(gateways) or "none")
    else:
        verifiers = build_verifiers(cfg) if cfg.payments_enabled else {}
        payment_flow = PaymentFlow(cfg, store, verifiers) if verifiers else None
        checkout_flow = None
        payment_labels = [v.label for v in verifiers.values()]

    system_prompt = build_system_prompt(
        cfg.business_name, cfg.catalog, cfg.checkout_url,
        payment_methods=payment_labels,
        persona_name=cfg.persona_name,
        persona_style=cfg.persona_style,
    )

    app = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("terms", terms_cmd))
    app.add_handler(CommandHandler("leads", leads_cmd))
    if checkout_flow is not None:
        checkout_flow.register(app)
    elif payment_flow is not None:
        payment_flow.register(app)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Starting %s sales bot…", cfg.business_name)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
