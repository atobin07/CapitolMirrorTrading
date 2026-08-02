"""Telegram buy/pay flow with payment-ID verification.

Deterministic and button-driven on purpose: the LLM handles *selling*, but it
never decides whether money arrived. Payment state moves only through these
handlers and provider APIs (or explicit human approval).

Flow:
  /buy → pick product → pick provider → pay → send payment ID →
      verify (API) ─ verified ─────────────▶ deliver
                   └ manual/mismatch/reused ▶ seller Approve/Reject ▶ deliver
"""
from __future__ import annotations

import logging
from decimal import Decimal

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app.payments.base import (
    AMOUNT_MISMATCH,
    CURRENCY_MISMATCH,
    REUSED,
    VERIFIED,
)
from app.sales import format_price

log = logging.getLogger(__name__)


class PaymentFlow:
    def __init__(self, cfg, store, verifiers: dict) -> None:
        self.cfg = cfg
        self.store = store
        self.verifiers = verifiers
        self.currency = cfg.catalog.get("currency", "USD")

    # ── registration ─────────────────────────────────────────────────────
    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("buy", self.cmd_buy))
        app.add_handler(CommandHandler("orders", self.cmd_orders))
        app.add_handler(CallbackQueryHandler(self.on_buy, pattern=r"^buy:"))
        app.add_handler(CallbackQueryHandler(self.on_pick_provider, pattern=r"^pay:"))
        app.add_handler(CallbackQueryHandler(self.on_cancel, pattern=r"^cancel:"))
        app.add_handler(CallbackQueryHandler(self.on_seller_decision, pattern=r"^(approve|reject):"))

    # ── helpers ──────────────────────────────────────────────────────────
    def _product(self, product_id: str) -> dict | None:
        for p in self.cfg.catalog.get("products", []):
            if str(p.get("id")) == str(product_id):
                return p
        return None

    def _who(self, order) -> str:
        return order["username"] or order["full_name"] or f"id:{order['chat_id']}"

    def _is_seller(self, user_id: int) -> bool:
        return user_id in self.cfg.seller_chat_ids

    async def _notify_sellers(self, context, text, markup=None):
        for sid in self.cfg.seller_chat_ids:
            try:
                await context.bot.send_message(
                    sid, text, parse_mode="Markdown", reply_markup=markup
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Notify seller %s failed: %s", sid, exc)

    # ── /buy ─────────────────────────────────────────────────────────────
    async def cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        products = self.cfg.catalog.get("products", [])
        if not products:
            await update.message.reply_text("No products are available right now.")
            return
        rows = [
            [InlineKeyboardButton(
                f"{p['name']} — {format_price(p.get('price'), self.currency)}",
                callback_data=f"buy:{p['id']}",
            )]
            for p in products
        ]
        await update.message.reply_text(
            "🛒 What would you like to buy?",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def on_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        product_id = query.data.split(":", 1)[1]
        product = self._product(product_id)
        if not product:
            await query.edit_message_text("Sorry, that product is no longer available.")
            return

        user = query.from_user
        username = f"@{user.username}" if user.username else ""
        order_id = self.store.create_order(
            chat_id=query.message.chat_id,
            username=username,
            full_name=user.full_name,
            product_id=str(product_id),
            product_name=product["name"],
            amount=str(product.get("price")),
            currency=self.currency,
        )

        if not self.verifiers:
            await query.edit_message_text(
                "Payments aren't set up yet — our team will reach out to finalize."
            )
            await self._notify_sellers(
                context,
                f"🛒 *Order #{order_id}* for *{product['name']}* — payments not "
                f"configured. Follow up with {username or user.full_name}.",
            )
            return

        rows = [
            [InlineKeyboardButton(
                f"Pay with {v.label}", callback_data=f"pay:{order_id}:{key}"
            )]
            for key, v in self.verifiers.items()
        ]
        rows.append([InlineKeyboardButton("✖ Cancel", callback_data=f"cancel:{order_id}")])
        price = format_price(product.get("price"), self.currency)
        await query.edit_message_text(
            f"*{product['name']}* — *{price}*\n{product.get('summary', '')}\n\n"
            "How would you like to pay?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def on_pick_provider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        _, order_id_s, provider = query.data.split(":", 2)
        order_id = int(order_id_s)
        order = self.store.get_order(order_id)
        if not order or order["state"] in self.store._TERMINAL:
            await query.edit_message_text("This order is no longer active. Send /buy to start over.")
            return
        verifier = self.verifiers.get(provider)
        if not verifier:
            await query.edit_message_text("That payment method isn't available.")
            return

        self.store.set_order_state(order_id, "awaiting_id", provider=provider)
        instructions = verifier.payment_instructions(order["amount"], order["currency"])
        await query.edit_message_text(
            f"{instructions}\n\n"
            "When you're done, *send me the payment ID here* and I'll confirm it.",
            parse_mode="Markdown",
        )

    async def on_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        order_id = int(query.data.split(":", 1)[1])
        self.store.set_order_state(order_id, "cancelled")
        await query.edit_message_text("Order cancelled. Send /buy whenever you're ready.")

    # ── payment-ID handling (called from bot.on_message) ─────────────────
    async def maybe_handle_payment_id(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """If the chat has an order awaiting a payment ID, process the text as
        that ID and return True. Otherwise return False so the LLM can reply."""
        chat_id = update.effective_chat.id
        order = self.store.active_order_for_chat(chat_id)
        if not order or order["state"] != "awaiting_id":
            return False

        payment_id = (update.message.text or "").strip()
        # Guard against obviously-not-an-ID chatter.
        if len(payment_id) < 4 or len(payment_id) > 120 or " " in payment_id.strip():
            await update.message.reply_text(
                "Please send just the payment/transaction ID (no extra text)."
            )
            return True

        provider = order["provider"]
        verifier = self.verifiers.get(provider)
        if not verifier:
            await update.message.reply_text("That payment method isn't available anymore.")
            return True

        # Fast reuse check before hitting the provider API.
        if self.store.is_payment_consumed(provider, payment_id):
            await update.message.reply_text(
                "That payment ID has already been used. Our team will take a look "
                "if you think that's a mistake."
            )
            await self._flag_for_review(context, order, payment_id, "REUSED payment id")
            return True

        await update.message.chat.send_action("typing")
        result = await verifier.verify(
            payment_id, Decimal(str(order["amount"])), order["currency"]
        )

        if result.verified:
            # Atomically claim the id; if we lose the race, treat as reused.
            if not self.store.consume_payment(provider, payment_id, order["id"]):
                await update.message.reply_text(
                    "That payment ID has already been used."
                )
                return True
            self.store.set_order_state(
                order["id"], "paid", payment_id=payment_id, reason="auto-verified"
            )
            await update.message.reply_text(result.customer_message())
            await self._deliver(context, order)
            await self._notify_sellers(
                context,
                f"✅ *Payment verified* — Order #{order['id']} "
                f"({order['product_name']}) from {self._who(order)}.\n"
                f"{provider} • {result.amount} {result.currency} • id `{payment_id}`",
            )
            return True

        # Not auto-verified. Route reused/mismatch/manual to human approval;
        # let transient failures (not found / error) be retried.
        if result.status in (REUSED, AMOUNT_MISMATCH, CURRENCY_MISMATCH) or result.needs_manual_review:
            await update.message.reply_text(result.customer_message())
            await self._flag_for_review(
                context, order, payment_id, result.reason or result.status
            )
        else:
            # not_found / failed / error → keep awaiting_id so they can retry.
            await update.message.reply_text(result.customer_message())
        return True

    async def _flag_for_review(self, context, order, payment_id, reason) -> None:
        self.store.set_order_state(
            order["id"], "reviewing", payment_id=payment_id, reason=reason
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{order['id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{order['id']}"),
        ]])
        await self._notify_sellers(
            context,
            f"🕵️ *Needs review* — Order #{order['id']} ({order['product_name']}) "
            f"from {self._who(order)}\n"
            f"{order['provider']} • expected *{order['amount']} {order['currency']}* • "
            f"id `{payment_id}`\nReason: _{reason}_\n\n"
            "Verify the payment in your account, then decide:",
            markup,
        )

    # ── seller approve/reject ────────────────────────────────────────────
    async def on_seller_decision(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._is_seller(query.from_user.id):
            await query.answer("Not authorized.", show_alert=True)
            return
        await query.answer()
        action, order_id_s = query.data.split(":", 1)
        order_id = int(order_id_s)
        order = self.store.get_order(order_id)
        if not order:
            await query.edit_message_text("That order no longer exists.")
            return
        if order["state"] in self.store._TERMINAL:
            await query.edit_message_text(
                f"Order #{order_id} is already *{order['state']}*.",
                parse_mode="Markdown",
            )
            return

        if action == "approve":
            payment_id = order["payment_id"] or ""
            if payment_id and not self.store.consume_payment(
                order["provider"], payment_id, order_id
            ):
                await query.edit_message_text(
                    f"⚠️ Order #{order_id}: payment id `{payment_id}` was already "
                    "used on another order. Not approving.",
                    parse_mode="Markdown",
                )
                return
            self.store.set_order_state(order_id, "paid", reason="approved by seller")
            await query.edit_message_text(
                f"✅ Approved Order #{order_id} ({order['product_name']}).",
            )
            await self._deliver(context, order)
        else:  # reject
            self.store.set_order_state(order_id, "failed", reason="rejected by seller")
            await query.edit_message_text(
                f"❌ Rejected Order #{order_id} ({order['product_name']}).",
            )
            try:
                await context.bot.send_message(
                    order["chat_id"],
                    "We couldn't confirm your payment for that order. If you believe "
                    "this is an error, reply here and we'll help sort it out.",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Notify buyer failed: %s", exc)

    # ── delivery ─────────────────────────────────────────────────────────
    async def _deliver(self, context, order) -> None:
        product = self._product(order["product_id"])
        deliverable = (product or {}).get("deliverable", "").strip()
        if deliverable:
            msg = f"🎉 Here's your *{order['product_name']}*:\n\n{deliverable}"
        else:
            msg = (
                f"🎉 Payment confirmed for *{order['product_name']}*! "
                "Our team will get you set up right away."
            )
            await self._notify_sellers(
                context,
                f"📦 *Fulfill Order #{order['id']}* ({order['product_name']}) "
                f"for {self._who(order)} — payment confirmed.",
            )
        try:
            await context.bot.send_message(
                order["chat_id"], msg, parse_mode="Markdown"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Deliver to buyer failed: %s", exc)

    # ── /orders (seller only) ────────────────────────────────────────────
    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_seller(update.effective_user.id):
            return
        rows = self.store.recent_orders(limit=15)
        if not rows:
            await update.message.reply_text("No orders yet.")
            return
        lines = ["📒 *Recent orders:*"]
        for r in rows:
            lines.append(
                f"#{r['id']} {r['product_name']} — {r['amount']} {r['currency']} "
                f"• *{r['state']}* • {r['username'] or r['full_name']}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def close(self) -> None:
        for v in self.verifiers.values():
            try:
                await v.close()
            except Exception:  # noqa: BLE001
                pass
