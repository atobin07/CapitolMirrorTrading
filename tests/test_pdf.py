"""Tests for file (PDF) products + delivery. Run: python tests/test_pdf.py"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import products as pdl  # noqa: E402
from app.checkout_flow import CheckoutFlow  # noqa: E402
from app.payments.gateway import PaymentState  # noqa: E402
from app.store import Store  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def test_helpers():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "a.pdf"), "wb").write(b"%PDF-1.4 test")
    file_p = {"id": "x", "title": "Red One", "organization": "Acme", "tags": ["red"], "file": "a.pdf"}
    key_p = {"id": "y", "name": "Key Pack"}
    check(pdl.is_file_product(file_p) and not pdl.is_file_product(key_p), "detects file products")
    check(pdl.file_exists(file_p, d), "finds an existing file")
    check(not pdl.file_exists({"file": "missing.pdf"}, d), "missing file → False")
    check(pdl.product_title(file_p) == "Red One", "title from title field")
    check(pdl.product_title(key_p) == "Key Pack", "title falls back to name")
    groups = pdl.group_by_org([file_p, {"id": "z", "name": "N", "organization": "Acme"}])
    check(list(groups) == ["Acme"] and len(groups["Acme"]) == 2, "groups by organization")


class _Bot:
    def __init__(self):
        self.msgs = []
        self.docs = []

    async def send_message(self, chat_id, text, **kw):
        self.msgs.append((chat_id, text))

    async def send_document(self, chat_id, document=None, filename=None, caption=None, **kw):
        self.docs.append((chat_id, filename, caption, document.read() if document else b""))


class _PaidGW:
    key = "stripe"; label = "Card / Apple Pay / Cash App"
    async def poll(self, ref):
        # Unique settled-payment id per checkout, like real processors.
        return PaymentState("paid", amount=Decimal("20"), currency="USD", txn_ref=f"pi_{ref}")
    async def close(self):
        pass


async def test_file_delivery_e2e():
    d = tempfile.mkdtemp()
    pdf_path = os.path.join(d, "red-guide.pdf")
    open(pdf_path, "wb").write(b"%PDF-1.4 the goods")
    store = Store(os.path.join(d, "t.db"))
    cfg = SimpleNamespace(
        catalog={"currency": "USD", "products": [
            {"id": "redpack", "title": "Red PDF Pack", "organization": "Acme",
             "tags": ["red"], "price": 20, "file": "red-guide.pdf"}]},
        catalog_dir=d, seller_chat_ids=[7],
        stripe_poll_interval=8, stripe_session_timeout=1800,
    )
    flow = CheckoutFlow(cfg, store, {"stripe": _PaidGW()})

    product = cfg.catalog["products"][0]
    check(flow._available(product), "file product available when file exists")

    # No reservation is taken for a file product (unlimited).
    oid = store.create_order(42, "@b", "B", "redpack", "Red PDF Pack", "20", "USD")
    store.set_order_state(oid, "awaiting_payment", provider="stripe", payment_id="cs_x")

    app = SimpleNamespace(bot=_Bot())
    await flow._poll_once(app)

    order = store.get_order(oid)
    check(order["state"] == "paid", "order paid")
    check(len(app.bot.docs) == 1, "exactly one document delivered")
    cid, fname, caption, data = app.bot.docs[0]
    check(cid == 42 and fname == "red-guide.pdf", "correct file to correct buyer")
    check(data == b"%PDF-1.4 the goods", "the actual PDF bytes were sent")
    check(any("custom" in t.lower() for _, t in app.bot.msgs), "post-sale custom-work upsell sent")
    check(any(cid == 7 and "Sale" in t for cid, t in app.bot.msgs), "seller notified")

    # Same file can be sold again — no stock to run out of.
    o2 = store.create_order(43, "@c", "C", "redpack", "Red PDF Pack", "20", "USD")
    store.set_order_state(o2, "awaiting_payment", provider="stripe", payment_id="cs_y")
    await flow._poll_once(app)
    check(len(app.bot.docs) == 2, "file product resold (unlimited stock)")

    # Missing file → not delivered, flagged to seller.
    cfg.catalog["products"][0]["file"] = "gone.pdf"
    o3 = store.create_order(44, "@e", "E", "redpack", "Red PDF Pack", "20", "USD")
    store.set_order_state(o3, "awaiting_payment", provider="stripe", payment_id="cs_z")
    await flow._poll_once(app)
    check(store.get_order(o3)["state"] == "paid_no_stock", "missing file → paid_no_stock, not delivered")
    store.close()


async def main():
    test_helpers()
    await test_file_delivery_e2e()
    print(f"\nALL {_checks} PDF-PRODUCT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
