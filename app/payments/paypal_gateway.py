"""PayPal autonomous hosted-checkout gateway (Orders API v2).

Covers PayPal AND Venmo — Venmo is a funding source inside PayPal's checkout,
so it's the same order, verified the same way. Flow:

  create_checkout → POST /v2/checkout/orders (intent=CAPTURE)  → approve URL
  customer approves + pays on PayPal
  poll → GET  /v2/checkout/orders/{id}                          → status
       → if APPROVED: POST /v2/checkout/orders/{id}/capture     → COMPLETED
  → PaymentState(paid, amount, currency, capture_id)

Venmo shows up for eligible buyers (US, mobile, enabled on your PayPal account).
"""
from __future__ import annotations

import base64
import logging
import time
from decimal import Decimal, InvalidOperation

import httpx

from app.payments.gateway import (
    ERROR,
    EXPIRED,
    PAID,
    UNPAID,
    CheckoutLink,
    PaymentState,
)

log = logging.getLogger(__name__)

_BASES = {
    "live": "https://api-m.paypal.com",
    "sandbox": "https://api-m.sandbox.paypal.com",
}


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return None


class PayPalGateway:
    key = "paypal"
    label = "PayPal / Venmo"

    def __init__(
        self,
        client_id: str,
        secret: str,
        *,
        env: str = "live",
        return_url: str = "https://t.me",
        cancel_url: str = "https://t.me",
        brand_name: str = "",
        enable_venmo: bool = True,
        timeout: int = 30,
    ) -> None:
        self.client_id = client_id
        self.secret = secret
        self.base = _BASES.get(env, _BASES["live"])
        self.live = env == "live"
        self.return_url = return_url
        self.cancel_url = cancel_url
        self.brand_name = brand_name
        self.enable_venmo = enable_venmo
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token = ""
        self._token_exp = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    # ── auth ─────────────────────────────────────────────────────────────
    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_exp:
            return self._token
        creds = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
        resp = await self._client.post(
            f"{self.base}/v1/oauth2/token",
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        d = resp.json()
        self._token = d["access_token"]
        self._token_exp = time.monotonic() + max(60, int(d.get("expires_in", 300)) - 60)
        return self._token

    async def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {await self._access_token()}",
            "Content-Type": "application/json",
        }

    # ── create ───────────────────────────────────────────────────────────
    async def create_checkout(
        self, *, product_name: str, amount, currency: str, order_id: int, chat_id: int
    ) -> CheckoutLink:
        value = f"{Decimal(str(amount)):.2f}"
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "custom_id": str(order_id),
                "description": product_name[:127],
                "amount": {"currency_code": currency.upper(), "value": value},
            }],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "return_url": self.return_url,
                        "cancel_url": self.cancel_url,
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                        **({"brand_name": self.brand_name} if self.brand_name else {}),
                    }
                }
            },
        }
        resp = await self._client.post(
            f"{self.base}/v2/checkout/orders", headers=await self._headers(), json=body
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"PayPal create order {resp.status_code}: {resp.text[:300]}")
        d = resp.json()
        ref = d.get("id", "")
        url = ""
        for link in d.get("links", []):
            if link.get("rel") in ("approve", "payer-action"):
                url = link.get("href", "")
                break
        if not url:
            raise RuntimeError("PayPal order created but no approve link returned")
        return CheckoutLink(ref=ref, url=url)

    # ── poll (+ capture) ─────────────────────────────────────────────────
    async def poll(self, ref: str) -> PaymentState:
        try:
            resp = await self._client.get(
                f"{self.base}/v2/checkout/orders/{ref}", headers=await self._headers()
            )
        except httpx.HTTPError as exc:
            return PaymentState(ERROR, reason=str(exc))
        if resp.status_code == 404:
            return PaymentState(EXPIRED, reason="order not found")
        if resp.status_code >= 400:
            return PaymentState(ERROR, reason=f"{resp.status_code}: {resp.text[:200]}")

        d = resp.json()
        status = d.get("status")

        if status == "COMPLETED":
            return self._completed_state(d)
        if status == "APPROVED":
            return await self._capture(ref)
        if status in ("VOIDED", "PAYER_ACTION_REQUIRED_EXPIRED"):
            return PaymentState(EXPIRED, reason=f"status {status}")
        # CREATED / SAVED / PAYER_ACTION_REQUIRED → still waiting
        return PaymentState(UNPAID, reason=f"status {status}")

    async def _capture(self, ref: str) -> PaymentState:
        try:
            resp = await self._client.post(
                f"{self.base}/v2/checkout/orders/{ref}/capture",
                headers=await self._headers(),
            )
        except httpx.HTTPError as exc:
            return PaymentState(ERROR, reason=str(exc))
        # Already captured by a prior poll tick → re-read the order.
        if resp.status_code == 422 and "ORDER_ALREADY_CAPTURED" in resp.text:
            reread = await self._client.get(
                f"{self.base}/v2/checkout/orders/{ref}", headers=await self._headers()
            )
            if reread.status_code == 200:
                return self._completed_state(reread.json())
        if resp.status_code >= 400:
            return PaymentState(ERROR, reason=f"capture {resp.status_code}: {resp.text[:200]}")
        return self._completed_state(resp.json())

    @staticmethod
    def _completed_state(order: dict) -> PaymentState:
        units = order.get("purchase_units", [])
        captures = (units[0].get("payments", {}).get("captures", []) if units else [])
        cap = captures[0] if captures else {}
        amt = cap.get("amount") or (units[0].get("amount", {}) if units else {})
        # A capture can itself be PENDING (e.g. review); only COMPLETED counts.
        cap_status = cap.get("status", "COMPLETED")
        if cap_status not in ("COMPLETED", "CAPTURED"):
            return PaymentState(UNPAID, reason=f"capture {cap_status}")
        return PaymentState(
            PAID,
            amount=_dec(amt.get("value")),
            currency=(amt.get("currency_code") or "").upper() or None,
            txn_ref=cap.get("id") or order.get("id"),
        )

    async def ping(self) -> tuple[bool, str]:
        try:
            await self._access_token()
            return True, f"authenticated ({'LIVE' if self.live else 'sandbox'})"
        except httpx.HTTPError as exc:
            return False, str(exc)
