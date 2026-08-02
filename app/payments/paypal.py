"""PayPal payment verification via the official REST API.

Verification strategy, in order:
  1. Treat the ID as a capture ID:  GET /v2/payments/captures/{id}
  2. Treat the ID as an order ID:    GET /v2/checkout/orders/{id}
  3. Transaction Search (received payments): GET /v1/reporting/transactions

For (3) to work you must enable "Transaction Search" on your REST app in the
PayPal developer dashboard. Transaction Search data can lag a few minutes.
"""
from __future__ import annotations

import base64
import logging
import time
from decimal import Decimal, InvalidOperation

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
    "live": "https://api-m.paypal.com",
    "sandbox": "https://api-m.sandbox.paypal.com",
}


def _to_decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


class PayPalVerifier(PaymentVerifier):
    key = "paypal"
    label = "PayPal"
    manual = False

    def __init__(
        self,
        client_id: str,
        secret: str,
        *,
        env: str = "live",
        paypal_me: str = "",
        timeout: int = 30,
    ) -> None:
        self.client_id = client_id
        self.secret = secret
        self.base = _BASES.get(env, _BASES["live"])
        self.paypal_me = paypal_me.strip().lstrip("@")
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token = ""
        self._token_exp = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    def payment_instructions(self, amount, currency: str) -> str:
        if self.paypal_me:
            base = self.paypal_me
            url = base if base.startswith("http") else f"https://paypal.me/{base}"
            link = f"{url}/{amount}" if "/" not in base.split("paypal.me/")[-1] else url
            return (
                f"💙 *PayPal* — pay *{amount} {currency}* here:\n{link}\n"
                "After paying, open your PayPal activity, copy the *Transaction ID*, "
                "and send it to me."
            )
        return (
            f"💙 *PayPal* — send *{amount} {currency}*, then copy the "
            "*Transaction ID* from your PayPal activity and send it to me."
        )

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_exp:
            return self._token
        creds = base64.b64encode(
            f"{self.client_id}:{self.secret}".encode()
        ).decode()
        resp = await self._client.post(
            f"{self.base}/v1/oauth2/token",
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        # Refresh a minute before expiry.
        self._token_exp = time.monotonic() + max(60, int(data.get("expires_in", 300)) - 60)
        return self._token

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        token = await self._access_token()
        return await self._client.get(
            f"{self.base}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params=params,
        )

    def _result(self, payment_id, status, **kw) -> VerificationResult:
        return VerificationResult(
            provider=self.key, payment_id=payment_id, status=status, **kw
        )

    def _check_amount(
        self, payment_id, paid: Decimal | None, cur: str | None,
        expected: Decimal, currency: str, payer: str, raw: dict,
    ) -> VerificationResult:
        if cur and cur.upper() != currency.upper():
            return self._result(
                payment_id, CURRENCY_MISMATCH, amount=paid, currency=cur,
                payer=payer, raw=raw,
                reason=f"paid in {cur}, expected {currency}",
            )
        if paid is None or paid < expected:
            return self._result(
                payment_id, AMOUNT_MISMATCH, amount=paid, currency=cur,
                payer=payer, raw=raw,
                reason=f"paid {paid}, expected >= {expected}",
            )
        return self._result(
            payment_id, VERIFIED, verified=True, amount=paid, currency=cur,
            payer=payer, raw=raw,
        )

    async def verify(
        self, payment_id: str, expected_amount: Decimal, currency: str
    ) -> VerificationResult:
        payment_id = payment_id.strip()
        try:
            # 1) Capture ID
            resp = await self._get(f"/v2/payments/captures/{payment_id}")
            if resp.status_code == 200:
                d = resp.json()
                if d.get("status") != "COMPLETED":
                    return self._result(
                        payment_id, FAILED, raw=d,
                        reason=f"capture status {d.get('status')}",
                    )
                amt = d.get("amount", {})
                payer = (
                    d.get("payer", {}).get("email_address")
                    or d.get("supplementary_data", {}).get("related_ids", {}).get("order_id", "")
                )
                return self._check_amount(
                    payment_id, _to_decimal(amt.get("value")),
                    amt.get("currency_code"), expected_amount, currency,
                    payer, d,
                )

            # 2) Order ID
            resp = await self._get(f"/v2/checkout/orders/{payment_id}")
            if resp.status_code == 200:
                d = resp.json()
                if d.get("status") != "COMPLETED":
                    return self._result(
                        payment_id, FAILED, raw=d,
                        reason=f"order status {d.get('status')}",
                    )
                units = d.get("purchase_units", [])
                amt = units[0].get("amount", {}) if units else {}
                payer = d.get("payer", {}).get("email_address", "")
                return self._check_amount(
                    payment_id, _to_decimal(amt.get("value")),
                    amt.get("currency_code"), expected_amount, currency,
                    payer, d,
                )

            # 3) Transaction Search (received P2P/PayPal.me payments)
            return await self._search_transaction(
                payment_id, expected_amount, currency
            )
        except httpx.HTTPError as exc:
            log.warning("PayPal verify error for %s: %s", payment_id, exc)
            return self._result(payment_id, ERROR, reason=str(exc))

    async def _search_transaction(
        self, payment_id: str, expected: Decimal, currency: str
    ) -> VerificationResult:
        # Search a recent window; Transaction Search allows up to 31 days/page.
        # We look back 31 days which covers realistic "I just paid" cases.
        params = {
            "transaction_id": payment_id,
            "fields": "all",
            # start/end are required; use a wide-but-legal window relative to now.
            # PayPal wants ISO8601; we avoid Date.now-style calls here by letting
            # the caller-independent server clock drive "recent" via days=31.
            "start_date": _iso_days_ago(31),
            "end_date": _iso_now(),
        }
        resp = await self._get("/v1/reporting/transactions", params=params)
        if resp.status_code != 200:
            return self._result(
                payment_id, NOT_FOUND, raw={"http": resp.status_code},
                reason="not found as capture/order and transaction search "
                       f"returned {resp.status_code}",
            )
        d = resp.json()
        details = d.get("transaction_details", [])
        if not details:
            return self._result(
                payment_id, NOT_FOUND, raw=d,
                reason="no matching transaction found",
            )
        info = details[0].get("transaction_info", {})
        status = info.get("transaction_status")
        # S = success/completed for received payments.
        if status not in ("S", "P", "V", None):
            return self._result(
                payment_id, FAILED, raw=info,
                reason=f"transaction status {status}",
            )
        amt = info.get("transaction_amount", {})
        paid = _to_decimal(amt.get("value"))
        # Received payments are positive; refunds negative.
        if paid is not None:
            paid = abs(paid)
        return self._check_amount(
            payment_id, paid, amt.get("currency_code"),
            expected, currency,
            details[0].get("payer_info", {}).get("email_address", ""), info,
        )


# ── time helpers (avoid forbidden Date.now-style calls in workflow scope; this
#    is normal runtime code, so datetime is fine here) ────────────────────────
def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


def _iso_days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S-0000"
    )
