"""Autonomous multi-processor checkout + inventory delivery.

Flow (no human in the loop):
  /buy → pick product → reserve 1 stock item → pick processor (if more than one)
  → create a hosted checkout link → send it → background poller watches the
  processor's API → on paid: claim the reserved item from the DB and deliver it
  → on expiry: release the reservation.

Processors plug in as gateways (Stripe for card/Apple Pay/Cash App, PayPal for
PayPal/Venmo); they share one interface (app/payments/gateway.py), so the flow
treats them uniformly. The poller reconciles from the DB, so it survives
restarts: on startup it resumes watching every order still 'awaiting_payment'.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app import products as pdl
from app.sales import format_price

log = logging.getLogger(__name__)


class CheckoutFlow:
    def __init__(self, cfg, store, gateways: dict) -> None:
        self.cfg = cfg
        self.store = store
        # {provider_key: gateway}, e.g. {"stripe": ..., "paypal": ...}
        self.gateways = gateways
        self.currency = cfg.catalog.get("currency", "USD")
        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ── registration / lifecycle ─────────────────────────────────────────
    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("buy", self.cmd_buy))
        app.add_handler(CommandHandler("stock", self.cmd_stock))
        app.add_handler(CommandHandler("orders", self.cmd_orders))
        app.add_handler(CallbackQueryHandler(self.on_buy, pattern=r"^buy:"))
        app.add_handler(CallbackQueryHandler(self.on_pick_provider, pattern=r"^pay:"))
        app.add_handler(CallbackQueryHandler(self.on_cancel, pattern=r"^xcncl:"))

    async def start(self, app: Application) -> None:
        # Free items held by orders that never completed (e.g. crash mid-checkout).
        freed = self.store.release_stale_reservations()
        if freed:
            log.info("Released %d stale stock reservation(s).", freed)
        self._stop.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(app))
        log.info(
            "Checkout poller started (%s) — every %ss.",
            "/".join(self.gateways) or "no processors", self.cfg.stripe_poll_interval,
        )

    async def close(self) -> None:
        self._stop.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for gw in self.gateways.values():
            try:
                await gw.close()
            except Exception:  # noqa: BLE001
                pass

    # ── helpers ──────────────────────────────────────────────────────────
    def _product(self, product_id: str) -> dict | None:
        for p in self.cfg.catalog.get("products", []):
            if str(p.get("id")) == str(product_id):
                return p
        return None

    def _pname(self, product: dict) -> str:
        return pdl.product_title(product)

    def _available(self, product: dict) -> bool:
        """File products are unlimited (as long as the file exists); key/stock
        products depend on remaining inventory."""
        if pdl.is_file_product(product):
            return pdl.file_exists(product, self.cfg.catalog_dir)
        return self.store.available_count(str(product["id"])) > 0

    def _who(self, order) -> str:
        return order["username"] or order["full_name"] or f"id:{order['chat_id']}"

    def _via(self, order) -> str:
        source = self.store.get_source(order["chat_id"])
        return f" · via {source}" if source else ""

    def _is_seller(self, user_id: int) -> bool:
        return user_id in self.cfg.seller_chat_ids

    async def _notify_sellers(self, app, text) -> None:
        for sid in self.cfg.seller_chat_ids:
            try:
                await app.bot.send_message(sid, text, parse_mode="Markdown")
            except Exception as exc:  # noqa: BLE001
                log.warning("Notify seller %s failed: %s", sid, exc)

    # ── /buy ─────────────────────────────────────────────────────────────
    async def cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        products = self.cfg.catalog.get("products", [])
        if not products:
            await update.message.reply_text("No products are available right now.")
            return
        rows = []
        for p in products:
            label = f"{self._pname(p)} — {format_price(p.get('price'), self.currency)}"
            if not self._available(p):
                label += " (sold out)"
            rows.append([InlineKeyboardButton(label, callback_data=f"buy:{p['id']}")])
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

        if not self._available(product):
            await query.edit_message_text(
                f"😞 *{self._pname(product)}* isn't available right now. "
                "Check back soon!",
                parse_mode="Markdown",
            )
            return

        user = query.from_user
        username = f"@{user.username}" if user.username else ""
        order_id = self.store.create_order(
            chat_id=query.message.chat_id,
            username=username,
            full_name=user.full_name,
            product_id=str(product_id),
            product_name=self._pname(product),
            amount=str(product.get("price")),
            currency=self.currency,
        )

        # Unique-key products get one item reserved (prevents oversell). File
        # products (PDFs) are unlimited, so there's nothing to reserve.
        if not pdl.is_file_product(product):
            reserved = self.store.reserve_stock(str(product_id), order_id)
            if reserved is None:
                self.store.set_order_state(order_id, "cancelled", reason="out of stock")
                await query.edit_message_text(
                    f"😞 *{self._pname(product)}* just sold out. Sorry about that!",
                    parse_mode="Markdown",
                )
                return

        providers = list(self.gateways.items())
        # One processor → straight to its checkout. Several → let them choose.
        if len(providers) == 1:
            provider, gw = providers[0]
            await self._open_checkout(query, order_id, product, provider, gw)
            return

        price = format_price(product.get("price"), self.currency)
        rows = [[InlineKeyboardButton(gw.label, callback_data=f"pay:{order_id}:{key}")]
                for key, gw in providers]
        rows.append([InlineKeyboardButton("✖ Cancel", callback_data=f"xcncl:{order_id}")])
        await query.edit_message_text(
            f"*{product['name']}* — *{price}*\n\nHow would you like to pay?",
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
        gw = self.gateways.get(provider)
        product = self._product(order["product_id"])
        if not gw or not product:
            await query.edit_message_text("That payment method isn't available right now.")
            return
        await self._open_checkout(query, order_id, product, provider, gw)

    async def on_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        order_id = int(query.data.split(":", 1)[1])
        self.store.release_reservation(order_id)
        self.store.set_order_state(order_id, "cancelled", reason="cancelled by buyer")
        await query.edit_message_text("No worries — cancelled. Send /buy whenever.")

    async def _open_checkout(self, query, order_id, product, provider, gw) -> None:
        """Create a hosted checkout with `gw` and hand the buyer the pay link."""
        try:
            link = await gw.create_checkout(
                product_name=product["name"],
                amount=Decimal(str(product.get("price"))),
                currency=self.currency,
                order_id=order_id,
                chat_id=query.message.chat_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("%s checkout create failed: %s", provider, exc)
            self.store.release_reservation(order_id)
            self.store.set_order_state(order_id, "failed", reason=f"{provider}: {exc}")
            await query.edit_message_text(
                "Sorry, I couldn't open a checkout right now. Please try again shortly."
            )
            return

        self.store.set_order_state(
            order_id, "awaiting_payment", provider=provider, payment_id=link.ref
        )
        price = format_price(product.get("price"), self.currency)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay now", url=link.url)]])
        await query.edit_message_text(
            f"*{product['name']}* — *{price}*\n\n"
            f"Tap below to pay with {gw.label}. As soon as your payment clears, "
            "I'll send your item here automatically. 🔒",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    # ── background poller ────────────────────────────────────────────────
    async def _poll_loop(self, app: Application) -> None:
        interval = max(3, self.cfg.stripe_poll_interval)
        while not self._stop.is_set():
            try:
                await self._poll_once(app)
            except Exception as exc:  # noqa: BLE001
                log.warning("Checkout poll error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self, app: Application) -> None:
        # Also free reservations from orders that died since the last tick.
        self.store.release_stale_reservations()
        orders = self.store.orders_awaiting_payment()
        for order in orders:
            gw = self.gateways.get(order["provider"])
            if gw is None:
                continue
            state = await gw.poll(order["payment_id"])

            if state.is_paid:
                await self._fulfil(app, order, state)
            elif state.is_expired:
                await self._expire(app, order, state.reason or "checkout expired")
            else:
                # Still open (or a transient error) — expire locally past our window.
                age = time.time() - (order["created_at"] or time.time())
                if age > self.cfg.stripe_session_timeout:
                    await self._expire(app, order, "checkout timed out")

    async def _fulfil(self, app: Application, order, state) -> None:
        order_id = order["id"]
        provider = order["provider"]
        # Guard against double-processing the same settled payment.
        pay_ref = state.txn_ref or order["payment_id"]
        if not self.store.consume_payment(provider, pay_ref, order_id):
            return  # already handled by a previous poll tick

        # Defensive amount/currency check.
        expected = Decimal(str(order["amount"]))
        if (state.amount is not None and state.amount < expected) or (
            state.currency and state.currency.upper() != order["currency"].upper()
        ):
            self.store.set_order_state(
                order_id, "failed",
                reason=f"amount/currency mismatch: {state.amount} {state.currency}",
            )
            await self._notify_sellers(
                app,
                f"⚠️ Order #{order_id}: {provider} amount/currency mismatch "
                f"({state.amount} {state.currency}). Not delivered.",
            )
            return

        product = self._product(order["product_id"]) or {}

        # ── file products (PDFs): deliver the document; unlimited stock ──
        if pdl.is_file_product(product):
            if not await self._deliver_file(app, order["chat_id"], product, order):
                self.store.set_order_state(order_id, "paid_no_stock", reason="product file missing")
                await self._safe_send(
                    app, order["chat_id"],
                    "✅ Payment received! Your files are being prepared and will "
                    "arrive here shortly.",
                )
                await self._notify_sellers(
                    app,
                    f"🚨 *Order #{order_id} PAID but file missing* "
                    f"({order['product_name']}) — deliver manually.",
                )
                return
            self.store.set_order_state(order_id, "paid", reason=f"{provider} auto-verified")
            await self._post_sale(app, order["chat_id"])
            await self._notify_sellers(
                app,
                f"💰 *Sale* — Order #{order_id} {order['product_name']} "
                f"({order['amount']} {order['currency']}) delivered to "
                f"{self._who(order)}{self._via(order)}.",
            )
            return

        # ── unique-key / stock products ──
        content = self.store.consume_reserved_stock(order_id)
        if content is None:
            # Should not happen (we reserved up front), but never deliver nothing.
            self.store.set_order_state(order_id, "paid_no_stock", reason="reserved item missing")
            await self._safe_send(
                app, order["chat_id"],
                "✅ Payment received! Your item is being prepared and will arrive "
                "here shortly.",
            )
            await self._notify_sellers(
                app,
                f"🚨 *Order #{order_id} PAID but no stock to deliver* "
                f"({order['product_name']}) — add stock and deliver manually.",
            )
            return

        self.store.set_order_state(order_id, "paid", reason=f"{provider} auto-verified")
        await self._safe_send(
            app, order["chat_id"],
            f"✅ Payment confirmed — here's your *{order['product_name']}*:\n\n"
            f"{content}\n\nThank you! 🎉",
        )
        remaining = self.store.available_count(order["product_id"])
        await self._notify_sellers(
            app,
            f"💰 *Sale* — Order #{order_id} {order['product_name']} "
            f"({order['amount']} {order['currency']}) delivered to "
            f"{self._who(order)}{self._via(order)}.\n"
            f"Stock left: {remaining}"
            + ("  ⚠️ *restock soon*" if remaining <= 2 else ""),
        )

    async def _deliver_file(self, app, chat_id, product, order) -> bool:
        """Send the product's PDF as a Telegram document. Returns success."""
        path = pdl.resolve_path(product, self.cfg.catalog_dir)
        if not path or not os.path.isfile(path):
            log.warning("Product file missing for order %s: %s", order["id"], path)
            return False
        try:
            with open(path, "rb") as fh:
                await app.bot.send_document(
                    chat_id, document=fh, filename=os.path.basename(path),
                    caption=f"✅ Payment confirmed — here's your "
                            f"{order['product_name']} 🎉",
                )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Deliver file failed for order %s: %s", order["id"], exc)
            return False

    async def _post_sale(self, app, chat_id) -> None:
        """One casual thank-you + custom-work mention after delivery."""
        await self._safe_send(
            app, chat_id,
            "thanks 🙌 got more whenever you want — and i can do custom pieces "
            "made to order (runs a bit more). just say the word.",
        )

    async def _expire(self, app: Application, order, reason: str) -> None:
        self.store.release_reservation(order["id"])
        self.store.set_order_state(order["id"], "expired", reason=reason)
        await self._safe_send(
            app, order["chat_id"],
            "⌛ Your checkout link expired before payment completed. "
            "Send /buy to try again — your item wasn't charged.",
        )

    async def _safe_send(self, app, chat_id, text) -> None:
        try:
            await app.bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as exc:  # noqa: BLE001
            log.warning("Send to %s failed: %s", chat_id, exc)

    # ── seller commands ──────────────────────────────────────────────────
    async def cmd_stock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_seller(update.effective_user.id):
            return
        rows = self.store.stock_summary()
        if not rows:
            await update.message.reply_text(
                "No stock loaded. Add some with:\n"
                "`python run.py stock <product_id> <file.txt>`",
                parse_mode="Markdown",
            )
            return
        lines = ["📦 *Inventory:*"]
        for r in rows:
            product = self._product(r["product_id"])
            name = product["name"] if product else r["product_id"]
            lines.append(
                f"• {name}: *{r['available'] or 0}* available "
                f"({r['reserved'] or 0} reserved, {r['consumed'] or 0} sold)"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

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
