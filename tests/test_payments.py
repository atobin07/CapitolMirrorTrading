"""Tests for payment verification. Run: python tests/test_payments.py

Uses httpx.MockTransport so no real PayPal/Square calls are made.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from decimal import Decimal

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.payments.base import (  # noqa: E402
    AMOUNT_MISMATCH,
    CURRENCY_MISMATCH,
    NOT_FOUND,
    PENDING_REVIEW,
    VERIFIED,
)
from app.payments.manual import ManualVerifier  # noqa: E402
from app.payments.paypal import PayPalVerifier  # noqa: E402
from app.payments.square import SquareApplePayVerifier, SquareCashAppVerifier  # noqa: E402
from app.store import Store  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── Square (Cash App / Apple Pay share the API) ──────────────────────────────
async def test_square():
    def handler(request: httpx.Request) -> httpx.Response:
        pid = request.url.path.rsplit("/", 1)[-1]
        table = {
            "ok": {"status": "COMPLETED", "amount_money": {"amount": 4900, "currency": "USD"}},
            "low": {"status": "COMPLETED", "amount_money": {"amount": 1000, "currency": "USD"}},
            "eur": {"status": "COMPLETED", "amount_money": {"amount": 4900, "currency": "EUR"}},
            "pending": {"status": "PENDING", "amount_money": {"amount": 4900, "currency": "USD"}},
        }
        if pid == "missing":
            return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND"}]})
        return httpx.Response(200, json={"payment": table[pid]})

    v = SquareCashAppVerifier("tok")
    v._client = mock_client(handler)

    r = await v.verify("ok", Decimal("49"), "USD")
    check(r.verified and r.status == VERIFIED, "square exact amount verifies")
    check(r.amount == Decimal("49"), "square parses cents→dollars")

    r = await v.verify("low", Decimal("49"), "USD")
    check(not r.verified and r.status == AMOUNT_MISMATCH, "square underpay rejected")

    r = await v.verify("eur", Decimal("49"), "USD")
    check(r.status == CURRENCY_MISMATCH, "square wrong currency rejected")

    r = await v.verify("pending", Decimal("49"), "USD")
    check(not r.verified, "square non-complete status rejected")

    r = await v.verify("missing", Decimal("49"), "USD")
    check(r.status == NOT_FOUND, "square 404 → not_found")

    # Overpay is accepted (>= expected).
    r = await v.verify("ok", Decimal("40"), "USD")
    check(r.verified, "square overpay accepted")

    # Apple Pay uses the same verification path.
    ap = SquareApplePayVerifier("tok")
    ap._client = mock_client(handler)
    r = await ap.verify("ok", Decimal("49"), "USD")
    check(r.verified and r.provider == "applepay", "apple pay verifies via square")
    await v.close(); await ap.close()


# ── PayPal (capture path) ────────────────────────────────────────────────────
async def test_paypal():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "A", "expires_in": 300})
        if "/captures/" in path:
            pid = path.rsplit("/", 1)[-1]
            if pid == "good":
                return httpx.Response(200, json={
                    "status": "COMPLETED",
                    "amount": {"value": "149.00", "currency_code": "USD"},
                    "payer": {"email_address": "buyer@example.com"},
                })
            if pid == "denied":
                return httpx.Response(200, json={"status": "DECLINED",
                    "amount": {"value": "149.00", "currency_code": "USD"}})
            return httpx.Response(404, json={})
        # order lookup + transaction search both miss
        return httpx.Response(404, json={})

    v = PayPalVerifier("id", "secret", env="sandbox")
    v._client = mock_client(handler)

    r = await v.verify("good", Decimal("149"), "USD")
    check(r.verified and r.payer == "buyer@example.com", "paypal capture verifies")

    r = await v.verify("good", Decimal("200"), "USD")
    check(r.status == AMOUNT_MISMATCH, "paypal underpay vs 200 rejected")

    r = await v.verify("denied", Decimal("149"), "USD")
    check(not r.verified, "paypal declined capture rejected")
    await v.close()


# ── Manual (Venmo / P2P Cash App) ────────────────────────────────────────────
async def test_manual():
    v = ManualVerifier("venmo", "Venmo", handle="@shop")
    r = await v.verify("note123", Decimal("49"), "USD")
    check(r.status == PENDING_REVIEW and not r.verified, "manual → pending review")
    check("Venmo" in v.payment_instructions("49", "USD"), "manual instructions render")


# ── Ledger + order state machine ─────────────────────────────────────────────
def test_store():
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    s = Store(db)
    oid = s.create_order(1, "@a", "A", "pro", "Pro", "149", "USD")
    check(s.active_order_for_chat(1)["id"] == oid, "active order found")

    s.set_order_state(oid, "awaiting_id", provider="paypal")
    check(s.active_order_for_chat(1)["state"] == "awaiting_id", "state transition")

    check(s.consume_payment("paypal", "TX1", oid) is True, "first consume ok")
    check(s.consume_payment("paypal", "TX1", oid) is False, "reuse blocked")
    check(s.is_payment_consumed("paypal", "TX1") is True, "consumed flag set")
    check(s.is_payment_consumed("paypal", "TX2") is False, "unknown id not consumed")
    # Same id, different provider is a different payment.
    check(s.consume_payment("cashapp", "TX1") if False else s.consume_payment("cashapp", "TX1", oid), "cross-provider id allowed")

    s.set_order_state(oid, "paid", payment_id="TX1")
    check(s.active_order_for_chat(1) is None, "paid order no longer active")

    # New order cancels prior open ones.
    o2 = s.create_order(1, "@a", "A", "starter", "Starter", "49", "USD")
    o3 = s.create_order(1, "@a", "A", "pro", "Pro", "149", "USD")
    check(s.get_order(o2)["state"] == "cancelled", "new order cancels prior open order")
    check(s.get_order(o3)["state"] == "pending", "latest order pending")
    s.close()


async def main():
    await test_square()
    await test_paypal()
    await test_manual()
    test_store()
    print(f"\nALL {_checks} PAYMENT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
