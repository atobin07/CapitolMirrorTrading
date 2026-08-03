"""Peer-to-peer pay links (Cash App / Venmo / PayPal.me).

These build a button that opens the customer's own payment app, pre-filled with
the recipient and amount. IMPORTANT: a deep link only *starts* a P2P transfer —
Cash App and Venmo give no API, so the bot cannot confirm the money arrived from
the link alone. Confirmation is a separate step (seller approves, or an emailed
receipt is matched). To make matching possible, each order is assigned a unique
amount via `unique_amount()`.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


# key → (label, needs handle)
APPS = {
    "cashapp": "Cash App",
    "venmo": "Venmo",
    "paypal": "PayPal",
}


def app_label(key: str) -> str:
    return APPS.get(key, key)


def unique_amount(base_price, order_id: int) -> Decimal:
    """Give each pending order a distinct amount so an incoming payment can be
    matched to it. Adds 1–89 cents based on the order id."""
    base = Decimal(str(base_price)).quantize(Decimal("0.01"))
    offset = Decimal((order_id % 89) + 1) / Decimal(100)
    return (base + offset).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def order_note(order_id: int) -> str:
    """A short reference the buyer can include in the payment note."""
    return f"ORD{order_id:04d}"


def _fmt(amount) -> str:
    return f"{Decimal(str(amount)).quantize(Decimal('0.01'))}"


def pay_link(app: str, handle: str, amount, note: str = "") -> str | None:
    """Build a deep link that opens the payer's app pre-filled. None if the app
    or handle is missing/unsupported."""
    handle = (handle or "").strip()
    if not handle:
        return None
    amt = _fmt(amount)
    if app == "cashapp":
        tag = handle.lstrip("$")
        return f"https://cash.app/${tag}/{amt}"
    if app == "paypal":
        user = handle.split("paypal.me/")[-1].strip("/").lstrip("@")
        return f"https://paypal.me/{user}/{amt}"
    if app == "venmo":
        user = handle.lstrip("@")
        link = f"https://venmo.com/{user}?txn=pay&amount={amt}"
        if note:
            link += f"&note={note}"
        return link
    return None


def pay_instructions(app: str, handle: str, amount, note: str) -> str:
    """Human-facing text telling the buyer exactly what to send."""
    label = app_label(app)
    return (
        f"Send *exactly ${_fmt(amount)}* on {label} to *{handle}* "
        f"(note: {note}). The exact amount is how I match your order — "
        "please don't round it."
    )
