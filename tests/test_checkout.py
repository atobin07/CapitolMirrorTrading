"""Tests for autonomous Stripe checkout + inventory. Run:
    python tests/test_checkout.py
Uses httpx.MockTransport — no real Stripe calls.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from decimal import Decimal

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.payments.stripe_gateway import StripeGateway  # noqa: E402
from app.store import Store  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def fresh_store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "t.db"))


# ── Stripe gateway (mocked) ──────────────────────────────────────────────────
async def test_gateway():
    state = {"paid": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/checkout/sessions") and request.method == "POST":
            from urllib.parse import parse_qs
            body = parse_qs(request.content.decode())
            check(body.get("line_items[0][price_data][unit_amount]") == ["14900"],
                  "amount converted to cents in request")
            check(body.get("payment_method_types[0]") == ["card"], "card method sent")
            return httpx.Response(200, json={
                "id": "cs_test_1", "url": "https://checkout.stripe.com/pay/cs_test_1",
                "status": "open", "payment_status": "unpaid",
                "amount_total": 14900, "currency": "usd", "payment_intent": None,
            })
        if "/checkout/sessions/cs_test_1" in path:
            if state["paid"]:
                return httpx.Response(200, json={
                    "id": "cs_test_1", "url": "x", "status": "complete",
                    "payment_status": "paid", "amount_total": 14900,
                    "currency": "usd", "payment_intent": "pi_123",
                })
            return httpx.Response(200, json={
                "id": "cs_test_1", "url": "x", "status": "open",
                "payment_status": "unpaid", "amount_total": 14900,
                "currency": "usd", "payment_intent": None,
            })
        return httpx.Response(404, json={})

    gw = StripeGateway("sk_test_x", payment_methods=["card", "cashapp"])
    gw._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    s = await gw.create_session(
        product_name="Pro", amount=Decimal("149"), currency="USD",
        order_id=1, chat_id=99,
    )
    check(s.id == "cs_test_1" and s.url.startswith("https://"), "session created with url")
    check(not s.is_paid, "new session not paid")

    s2 = await gw.get_session("cs_test_1")
    check(not s2.is_paid, "still unpaid before payment")
    state["paid"] = True
    s3 = await gw.get_session("cs_test_1")
    check(s3.is_paid and s3.payment_intent == "pi_123", "detects paid + payment_intent")
    check(s3.amount_total == Decimal("149"), "cents parsed back to dollars")
    await gw.close()


# ── inventory: atomic reservation, no oversell ───────────────────────────────
def test_inventory():
    s = fresh_store()
    added = s.add_stock("pro", ["KEY-A", "KEY-B", "KEY-A"])  # dup ignored
    check(added == 2, "duplicate stock skipped")
    check(s.available_count("pro") == 2, "two available")

    o1 = s.create_order(1, "@a", "A", "pro", "Pro", "149", "USD")
    o2 = s.create_order(2, "@b", "B", "pro", "Pro", "149", "USD")
    r1 = s.reserve_stock("pro", o1)
    r2 = s.reserve_stock("pro", o2)
    check(r1 and r2 and r1 != r2, "two reservations get distinct items")
    check(s.available_count("pro") == 0, "none available after reserving both")

    o3 = s.create_order(3, "@c", "C", "pro", "Pro", "149", "USD")
    check(s.reserve_stock("pro", o3) is None, "no oversell when empty")

    # Release one; it becomes available again.
    s.release_reservation(o2)
    check(s.available_count("pro") == 1, "released reservation returns to pool")

    # Consume o1's reserved item.
    content = s.consume_reserved_stock(o1)
    check(content == r1, "consume returns the reserved item")
    check(s.consume_reserved_stock(o1) is None, "can't consume twice")

    summary = {r["product_id"]: r for r in s.stock_summary()}
    check(summary["pro"]["consumed"] == 1, "summary counts consumed")
    s.close()


# ── stale reservation cleanup on restart ─────────────────────────────────────
def test_stale_release():
    s = fresh_store()
    s.add_stock("pro", ["K1"])
    o = s.create_order(1, "@a", "A", "pro", "Pro", "149", "USD")
    s.reserve_stock("pro", o)
    s.set_order_state(o, "cancelled")  # order died mid-checkout
    check(s.available_count("pro") == 0, "reserved while cancelled")
    freed = s.release_stale_reservations()
    check(freed == 1 and s.available_count("pro") == 1, "startup frees stale reservation")
    s.close()


# ── double-spend guard on the payment ref ────────────────────────────────────
def test_payment_ledger():
    s = fresh_store()
    o = s.create_order(1, "@a", "A", "pro", "Pro", "149", "USD")
    check(s.consume_payment("stripe", "pi_1", o) is True, "first fulfil claims payment")
    check(s.consume_payment("stripe", "pi_1", o) is False, "second poll tick is a no-op")
    s.close()


# ── end-to-end fulfil path (poll → paid → deliver from DB) ───────────────────
class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _FakeApp:
    def __init__(self):
        self.bot = _FakeBot()


class _FakeGateway:
    """Returns a paid session for any id."""
    def __init__(self):
        self.live = True

    async def get_session(self, sid):
        from app.payments.stripe_gateway import CheckoutSession
        return CheckoutSession(
            id=sid, url="x", status="complete", payment_status="paid",
            amount_total=Decimal("149"), currency="USD",
            payment_intent="pi_e2e", raw={},
        )

    async def expire_session(self, sid):
        pass

    async def close(self):
        pass


async def test_fulfil_e2e():
    from types import SimpleNamespace

    from app.checkout_flow import CheckoutFlow

    s = fresh_store()
    s.add_stock("pro", ["SECRET-DELIVERABLE-XYZ"])
    cfg = SimpleNamespace(
        catalog={"currency": "USD", "products": [
            {"id": "pro", "name": "Pro", "price": 149}]},
        seller_chat_ids=[777],
        stripe_poll_interval=8,
        stripe_session_timeout=1800,
    )
    flow = CheckoutFlow(cfg, s, _FakeGateway())

    order_id = s.create_order(42, "@buyer", "Buyer", "pro", "Pro", "149", "USD")
    s.reserve_stock("pro", order_id)
    s.set_order_state(order_id, "awaiting_payment", provider="stripe", payment_id="cs_e2e")

    app = _FakeApp()
    await flow._poll_once(app)

    order = s.get_order(order_id)
    check(order["state"] == "paid", "order marked paid after poll")
    check(s.available_count("pro") == 0, "stock consumed on delivery")
    buyer_msgs = [t for cid, t in app.bot.sent if cid == 42]
    check(any("SECRET-DELIVERABLE-XYZ" in t for t in buyer_msgs),
          "buyer received the exact deliverable")
    check(any(cid == 777 for cid, _ in app.bot.sent), "seller notified of sale")

    # A second poll tick must not double-deliver or double-send.
    before = len(app.bot.sent)
    # Re-open would require another paid session; simulate a lingering poll by
    # forcing the order back to awaiting (payment already consumed in ledger).
    s.set_order_state(order_id, "awaiting_payment")
    await flow._poll_once(app)
    check(s.get_order(order_id)["state"] == "awaiting_payment",
          "already-consumed payment is a no-op (no re-fulfil)")
    check(len(app.bot.sent) == before, "no duplicate delivery on second tick")
    s.close()


async def main():
    await test_gateway()
    test_inventory()
    test_stale_release()
    test_payment_ledger()
    await test_fulfil_e2e()
    print(f"\nALL {_checks} CHECKOUT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
