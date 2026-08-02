"""Verification via the Square Payments API.

Square is a real payment processor, so any payment it processes has a Payment ID
we can look up to confirm status + amount. This backs two methods here:

  • Cash App  — via Square "Cash App Pay"
  • Apple Pay — via Square (Apple Pay is a card wallet Square accepts natively)

Both require a Square merchant account. What is NOT verifiable (no public API):
  • peer-to-peer $cashtag payments (personal Cash App)
  • Apple Cash (the iMessage person-to-person transfer)
Route those through the manual (human-approved) flow instead.
"""
from __future__ import annotations

import logging
from decimal import Decimal

import httpx

from app.payments.base import (
    AMOUNT_MISMATCH,
    CURRENCY_MISMATCH,
    ERROR,
    FAILED,
    NOT_FOUND,
    VERIFIED,
    PaymentVerifier,
    VerificationResult,
)

log = logging.getLogger(__name__)

_BASES = {
    "production": "https://connect.squareup.com",
    "sandbox": "https://connect.squareupsandbox.com",
}
_SQUARE_VERSION = "2024-10-17"


class SquarePaymentVerifier(PaymentVerifier):
    """Generic Square Payments verifier; subclasses set key/label/instructions."""

    manual = False

    def __init__(
        self,
        access_token: str,
        *,
        env: str = "production",
        pay_link: str = "",
        pay_hint: str = "",
        timeout: int = 30,
    ) -> None:
        self.access_token = access_token
        self.base = _BASES.get(env, _BASES["production"])
        self.pay_link = pay_link.strip()
        self.pay_hint = pay_hint.strip()
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    def payment_instructions(self, amount, currency: str) -> str:
        target = self.pay_link or self.pay_hint
        where = f"\n{target}" if target else ""
        return (
            f"*{self.label}* — pay *{amount} {currency}* via {self.label}:{where}\n"
            "After paying, send me the *Payment ID* from your receipt."
        )

    async def verify(
        self, payment_id: str, expected_amount: Decimal, currency: str
    ) -> VerificationResult:
        payment_id = payment_id.strip()
        try:
            resp = await self._client.get(
                f"{self.base}/v2/payments/{payment_id}",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Square-Version": _SQUARE_VERSION,
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            log.warning("Square verify error for %s: %s", payment_id, exc)
            return VerificationResult(self.key, payment_id, ERROR, reason=str(exc))

        if resp.status_code == 404:
            return VerificationResult(
                self.key, payment_id, NOT_FOUND, reason="no such payment id"
            )
        if resp.status_code != 200:
            return VerificationResult(
                self.key, payment_id, ERROR,
                reason=f"Square returned {resp.status_code}: {resp.text[:200]}",
            )

        payment = resp.json().get("payment", {})
        status = payment.get("status")
        if status not in ("COMPLETED", "APPROVED"):
            return VerificationResult(
                self.key, payment_id, FAILED, raw=payment,
                reason=f"payment status {status}",
            )

        money = payment.get("amount_money", {})
        # Square amounts are integer minor units (cents).
        paid = Decimal(money.get("amount", 0)) / Decimal(100)
        cur = money.get("currency")

        if cur and cur.upper() != currency.upper():
            return VerificationResult(
                self.key, payment_id, CURRENCY_MISMATCH, amount=paid, currency=cur,
                raw=payment, reason=f"paid in {cur}, expected {currency}",
            )
        if paid < expected_amount:
            return VerificationResult(
                self.key, payment_id, AMOUNT_MISMATCH, amount=paid, currency=cur,
                raw=payment, reason=f"paid {paid}, expected >= {expected_amount}",
            )

        return VerificationResult(
            self.key, payment_id, VERIFIED, verified=True, amount=paid,
            currency=cur, raw=payment,
        )


class SquareCashAppVerifier(SquarePaymentVerifier):
    key = "cashapp"
    label = "Cash App"

    def __init__(self, access_token, *, env="production", cashtag="", timeout=30):
        tag = cashtag.strip()
        if tag and not tag.startswith("$"):
            tag = f"${tag}"
        super().__init__(
            access_token, env=env,
            pay_hint=f"Send to {tag}" if tag else "",
            timeout=timeout,
        )

    def payment_instructions(self, amount, currency: str) -> str:
        hint = f"\n{self.pay_hint}" if self.pay_hint else ""
        return (
            f"💚 *Cash App* — send *{amount} {currency}*{hint}, then send me the "
            "*Payment ID* from your receipt."
        )


class SquareApplePayVerifier(SquarePaymentVerifier):
    key = "applepay"
    label = "Apple Pay"

    def payment_instructions(self, amount, currency: str) -> str:
        if self.pay_link:
            return (
                f"🍎 *Apple Pay* — pay *{amount} {currency}* here:\n{self.pay_link}\n"
                "It'll use Apple Pay at checkout. Afterward, send me the "
                "*Payment ID* from the confirmation."
            )
        return (
            f"🍎 *Apple Pay* — pay *{amount} {currency}* at our Apple Pay checkout, "
            "then send me the *Payment ID* from the confirmation."
        )
