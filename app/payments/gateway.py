"""Shared types for autonomous hosted-checkout gateways.

Every processor (Stripe, PayPal) implements the same tiny interface so the
checkout flow can treat them uniformly:

    link  = await gateway.create_checkout(...)   # -> CheckoutLink(ref, url)
    state = await gateway.poll(link.ref)          # -> PaymentState(...)

The bot sends `link.url` to the customer, then polls `state` until it's paid.
Verification is always the processor's API confirming payment — never the
customer's word.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

UNPAID = "unpaid"
PAID = "paid"
EXPIRED = "expired"
ERROR = "error"


@dataclass
class CheckoutLink:
    ref: str        # processor's id for this checkout (session/order id)
    url: str        # hosted pay page the customer opens


@dataclass
class PaymentState:
    status: str                      # unpaid | paid | expired | error
    amount: Decimal | None = None
    currency: str | None = None
    txn_ref: str | None = None       # unique settled-payment id (for the ledger)
    reason: str = ""

    @property
    def is_paid(self) -> bool:
        return self.status == PAID

    @property
    def is_expired(self) -> bool:
        return self.status == EXPIRED
