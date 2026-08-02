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

from app.config import Config
from app.ollama_client import OllamaClient, OllamaError
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
ollama: OllamaClient
system_prompt: str


def _display_name(update: Update) -> tuple[str, str]:
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else ""
    full_name = user.full_name if user else ""
    return username, full_name


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store.reset(update.effective_chat.id)
    greeting = (
        f"👋 Hey! Welcome to {cfg.business_name}. "
        "I'm here to help you find the right fit and answer any questions. "
        "What are you looking for today?"
    )
    await update.message.reply_text(greeting)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Just chat with me naturally — tell me what you need and I'll help.\n\n"
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

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    history = store.get_history(chat_id)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        reply = await ollama.chat(messages)
    except OllamaError as exc:
        log.error("Ollama error: %s", exc)
        await update.message.reply_text(
            "Sorry, I'm having a brief hiccup — give me a moment and try again."
        )
        return

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})
    store.save_history(chat_id, history, username, full_name)

    await update.message.reply_text(reply)

    # Capture + notify on strong buying intent.
    if detect_buying_signal(user_text):
        store.add_lead(chat_id, username, full_name, user_text)
        await _notify_sellers(context, update, user_text)


async def _post_init(app: Application) -> None:
    healthy = await ollama.health()
    if healthy:
        log.info("Ollama reachable; model '%s' is available.", cfg.ollama_model)
    else:
        log.warning(
            "Ollama model '%s' not found at %s. Pull it with "
            "`ollama pull %s` or the bot will error on every message.",
            cfg.ollama_model, cfg.ollama_host, cfg.ollama_model,
        )


async def _post_shutdown(app: Application) -> None:
    await ollama.close()
    store.close()


def main() -> None:
    global cfg, store, ollama, system_prompt
    cfg = Config.load()

    problems = cfg.validate()
    for p in problems:
        log.warning("Config: %s", p)
    if not cfg.telegram_token or cfg.telegram_token.startswith("123456:ABC"):
        raise SystemExit(
            "Cannot start: TELEGRAM_BOT_TOKEN is missing. "
            "Copy .env.example to .env and fill it in."
        )

    store = Store(cfg.database_path)
    ollama = OllamaClient(
        cfg.ollama_host,
        cfg.ollama_model,
        temperature=cfg.ollama_temperature,
        num_ctx=cfg.ollama_num_ctx,
        timeout=cfg.ollama_timeout,
    )
    system_prompt = build_system_prompt(
        cfg.business_name, cfg.catalog, cfg.checkout_url
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
    app.add_handler(CommandHandler("leads", leads_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Starting %s sales bot…", cfg.business_name)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
