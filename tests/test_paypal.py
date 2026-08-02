"""Tests for the PayPal/Venmo gateway + multi-processor checkout.
Run: python tests/test_paypal.py   (httpx mocked — no real PayPal calls)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from decimal import Decimal
from types import SimpleNamespace

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.checkout_flow import CheckoutFlow  # noqa: E402
from app.payments.gateway import PaymentState  # noqa: E402
from app.payments.paypal_gateway import PayPalGateway  # noqa: E402
from app.store import Store  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def fresh_store():
    return Store(os.path.join(tempfile.mkdtemp(), "t.db"))


async def test_paypal_gateway():
    # Order lifecycle: CREATED → APPROVED → (capture) → COMPLETED
    state = {"status": "CREATED", "captured": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 300})
        if path.endswith("/v2/checkout/orders") and request.method == "POST":
            body = request.content.decode()
            check('"149.00"' in body, "amount sent as 2dp string")
            check("CAPTURE" in body, "intent is CAPTURE")
            return httpx.Response(201, json={
                "id": "PPORDER1", "status": "CREATED",
                "links": [{"rel": "approve", "href": "https://paypal.com/checkoutnow?token=PPORDER1"}],
            })
        if path.endswith("/PPORDER1/capture") and request.method == "POST":
            state["captured"] = True
            state["status"] = "COMPLETED"
            return httpx.Response(201, json={
                "id": "PPORDER1", "status": "COMPLETED",
                "purchase_units": [{"payments": {"captures": [
                    {"id": "CAP123", "status": "COMPLETED",
                     "amount": {"currency_code": "USD", "value": "149.00"}}]}}],
            })
        if path.endswith("/checkout/orders/PPORDER1") and request.method == "GET":
            if state["status"] == "COMPLETED":
                return httpx.Response(200, json={
                    "id": "PPORDER1", "status": "COMPLETED",
                    "purchase_units": [{"payments": {"captures": [
                        {"id": "CAP123", "status": "COMPLETED",
                         "amount": {"currency_code": "USD", "value": "149.00"}}]}}],
                })
            return httpx.Response(200, json={"id": "PPORDER1", "status": state["status"]})
        return httpx.Response(404, json={})

    gw = PayPalGateway("id", "secret", env="sandbox")
    gw._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    link = await gw.create_checkout(
        product_name="Pro", amount=Decimal("149"), currency="USD",
        order_id=1, chat_id=9)
    check(link.ref == "PPORDER1" and "paypal.com" in link.url, "creates order + approve link")

    # Buyer hasn't approved yet → unpaid.
    st = await gw.poll("PPORDER1")
    check(not st.is_paid, "unpaid while still CREATED")

    # Buyer approves → poll captures and reports paid.
    state["status"] = "APPROVED"
    st = await gw.poll("PPORDER1")
    check(st.is_paid and st.txn_ref == "CAP123", "APPROVED → captured → paid")
    check(st.amount == Decimal("149.00") and st.currency == "USD", "amount/currency parsed")
    await gw.close()


class _PaidGW:
    def __init__(self, key, label):
        self.key, self.label = key, label

    async def create_checkout(self, **kw):
        from app.payments.gateway import CheckoutLink
        return CheckoutLink(ref=f"{self.key}_ref", url=f"https://pay/{self.key}")

    async def poll(self, ref):
        return PaymentState("paid", amount=Decimal("149"), currency="USD",
                            txn_ref=f"{self.key}_txn")

    async def close(self):
        pass


class _Q:
    def __init__(self, data, chat_id):
        self.data = data
        self.from_user = SimpleNamespace(id=1, username="u", full_name="U")
        self.message = SimpleNamespace(chat_id=chat_id)
        self.edited = None
        self.markup = None

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **kw):
        self.edited = text
        self.markup = kw.get("reply_markup")


async def test_two_processor_choice_and_pay():
    s = fresh_store()
    s.add_stock("pro", ["KEY-1"])
    cfg = SimpleNamespace(
        catalog={"currency": "USD", "products": [{"id": "pro", "name": "Pro", "price": 149}]},
        seller_chat_ids=[7], stripe_poll_interval=8, stripe_session_timeout=1800)
    gateways = {"stripe": _PaidGW("stripe", "Card / Apple Pay / Cash App"),
                "paypal": _PaidGW("paypal", "PayPal / Venmo")}
    flow = CheckoutFlow(cfg, s, gateways)

    # Pick product → two processors → choice buttons (not a pay link yet).
    q = _Q("buy:pro", 42)
    await flow.on_buy(SimpleNamespace(callback_query=q), None)
    labels = [b.callback_data for row in q.markup.inline_keyboard for b in row]
    check(any(c.startswith("pay:") and c.endswith(":stripe") for c in labels), "stripe option shown")
    check(any(c.endswith(":paypal") for c in labels), "paypal/venmo option shown")
    check(s.available_count("pro") == 0, "item reserved while choosing")

    order = s.active_order_for_chat(42)
    # Buyer picks PayPal → gets a pay link, provider recorded.
    q2 = _Q(f"pay:{order['id']}:paypal", 42)
    await flow.on_pick_provider(SimpleNamespace(callback_query=q2), None)
    pay_btns = [b.url for row in q2.markup.inline_keyboard for b in row if b.url]
    check(any("pay/paypal" in u for u in pay_btns), "paypal pay link handed over")
    check(s.get_order(order["id"])["provider"] == "paypal", "order tagged paypal")

    # Poller confirms + delivers from inventory.
    class _Bot:
        def __init__(self): self.sent = []
        async def send_message(self, cid, text, **k): self.sent.append((cid, text))
    app = SimpleNamespace(bot=_Bot())
    await flow._poll_once(app)
    check(s.get_order(order["id"])["state"] == "paid", "paypal order paid via poll")
    check(any("KEY-1" in t for cid, t in app.bot.sent if cid == 42), "delivered from stock")
    s.close()


async def main():
    await test_paypal_gateway()
    await test_two_processor_choice_and_pay()
    print(f"\nALL {_checks} PAYPAL/MULTI-PROCESSOR CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
