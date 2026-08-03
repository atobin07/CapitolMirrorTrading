"""Tests for the web storefront money + delivery path.
Run: python tests/test_webstore.py   (pure logic, no server needed)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import Store  # noqa: E402
from app.webstore import fulfil_web_order, process_event, verify_stripe_signature  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def test_stripe_signature():
    body = b'{"id":"evt_1"}'
    t = "1700000000"
    good = hmac.new(b"whsec", f"{t}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
    check(verify_stripe_signature("whsec", body, f"t={t},v1={good}") is True, "valid sig passes")
    check(verify_stripe_signature("whsec", body, f"t={t},v1=bad") is False, "bad sig fails")
    check(verify_stripe_signature("whsec", body, None) is False, "missing header fails")
    check(verify_stripe_signature("", body, None) is True, "no secret → dev skip")


def _order(store, price="20"):
    oid = store.create_order(111, "web", "", "redpack", "Red Pack", price, "USD")
    store.set_order_state(oid, "awaiting_payment", provider="stripe", payment_id="cs_web_1")
    return oid


def test_fulfil_and_download():
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    oid = _order(s)

    session = {"id": "cs_web_1", "amount_total": 2000, "payment_intent": "pi_1"}
    token = fulfil_web_order(s, session)
    check(token, "completed session → download token issued")
    check(s.get_order(oid)["state"] == "paid", "order marked paid")

    # Idempotent: a duplicate webhook returns the same token, doesn't re-pay.
    token2 = fulfil_web_order(s, session)
    check(token2 == token, "duplicate webhook is idempotent (same token)")

    # Token resolves to the paid order.
    row = s.resolve_download(token)
    check(row and row["id"] == oid, "download token resolves to the paid order")
    check(s.resolve_download("garbage") is None, "bad token resolves to nothing")

    # Underpayment is rejected.
    o2 = s.create_order(111, "web", "", "redpack", "Red Pack", "20", "USD")
    s.set_order_state(o2, "awaiting_payment", provider="stripe", payment_id="cs_web_2")
    under = fulfil_web_order(s, {"id": "cs_web_2", "amount_total": 500, "payment_intent": "pi_2"})
    check(under is None and s.get_order(o2)["state"] == "failed", "underpayment rejected")
    s.close()


def test_process_event():
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    _order(s)
    tok = process_event(s, {"type": "checkout.session.completed",
                            "data": {"object": {"id": "cs_web_1", "amount_total": 2000}}})
    check(tok, "checkout.session.completed fulfils the order")
    check(process_event(s, {"type": "payment_intent.created", "data": {"object": {}}}) is None,
          "unrelated events are ignored")
    s.close()


def main():
    test_stripe_signature()
    test_fulfil_and_download()
    test_process_event()
    print(f"\nALL {_checks} WEBSTORE CHECKS PASSED")


if __name__ == "__main__":
    main()
