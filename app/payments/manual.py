"""Manual (human-approved) verification.

Used for providers with no public verification API for personal accounts —
notably Venmo and peer-to-peer Cash App ($cashtag). The customer submits a
payment ID; it is recorded and a human approves or rejects it. NOTHING is
delivered until a human confirms.
"""
from __future__ import annotations

from decimal import Decimal

from app.payments.base import PENDING_REVIEW, PaymentVerifier, VerificationResult


class ManualVerifier(PaymentVerifier):
    manual = True

    def __init__(self, key: str, label: str, handle: str = "", note: str = "") -> None:
        self.key = key
        self.label = label
        self.handle = handle.strip()
        self.note = note

    def payment_instructions(self, amount, currency: str) -> str:
        to = f" to *{self.handle}*" if self.handle else ""
        extra = f"\n{self.note}" if self.note else ""
        return (
            f"💸 *{self.label}* — send *{amount} {currency}*{to}, then send me "
            f"the payment note / transaction ID from your receipt.{extra}"
        )

    async def verify(
        self, payment_id: str, expected_amount: Decimal, currency: str
    ) -> VerificationResult:
        # No API to check against — always route to a human.
        return VerificationResult(
            provider=self.key,
            payment_id=payment_id.strip(),
            status=PENDING_REVIEW,
            amount=expected_amount,
            currency=currency,
            reason="no verification API; awaiting human approval",
        )
