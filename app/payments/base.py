"""Base types shared by all payment verifiers."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


# Verification outcome status codes.
VERIFIED = "verified"              # provider confirmed a good payment
PENDING_REVIEW = "pending_review"  # no API; a human must approve
FAILED = "failed"                  # provider says not completed
NOT_FOUND = "not_found"            # provider has no such payment id
AMOUNT_MISMATCH = "amount_mismatch"
CURRENCY_MISMATCH = "currency_mismatch"
REUSED = "reused"                  # this id was already consumed
ERROR = "error"                    # could not reach/parse provider


@dataclass
class VerificationResult:
    provider: str
    payment_id: str
    status: str
    verified: bool = False
    amount: Decimal | None = None
    currency: str | None = None
    payer: str | None = None
    reason: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def needs_manual_review(self) -> bool:
        return self.status == PENDING_REVIEW

    def customer_message(self) -> str:
        """A safe, customer-facing explanation of the outcome."""
        if self.verified:
            return "✅ Payment confirmed — thank you!"
        if self.status == PENDING_REVIEW:
            return (
                "Thanks! I've logged your payment and it's being reviewed. "
                "You'll get a confirmation shortly."
            )
        if self.status == REUSED:
            return (
                "That payment ID has already been used. If you think this is a "
                "mistake, our team will take a look."
            )
        if self.status in (AMOUNT_MISMATCH, CURRENCY_MISMATCH):
            return (
                "I found that payment, but the amount doesn't match this order. "
                "Our team will review it and follow up."
            )
        if self.status == NOT_FOUND:
            return (
                "I couldn't find a payment with that ID yet. Double-check the ID, "
                "and note it can take a few minutes to appear after paying."
            )
        # FAILED / ERROR
        return (
            "I couldn't confirm that payment automatically. No worries — our team "
            "will verify it manually and follow up with you."
        )


class PaymentVerifier:
    """Interface every provider implements."""

    #: short key used in config/DB, e.g. "paypal", "cashapp", "venmo"
    key: str = ""
    #: human label, e.g. "PayPal"
    label: str = ""
    #: True when this provider has no verification API and relies on a human
    manual: bool = False

    def payment_instructions(self, amount, currency: str) -> str:
        """Text telling the customer how to pay this provider."""
        raise NotImplementedError

    async def verify(
        self, payment_id: str, expected_amount: Decimal, currency: str
    ) -> VerificationResult:
        raise NotImplementedError

    async def close(self) -> None:  # optional cleanup
        return None
