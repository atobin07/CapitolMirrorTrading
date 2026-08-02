"""Stripe hosted-checkout gateway for autonomous selling.

We create a Checkout Session (a hosted pay page supporting cards, Apple Pay,
Google Pay, and Cash App Pay), hand the customer the URL, then *poll* the
session until it's paid. Polling means no webhook endpoint / public domain /
open ports are needed — the bot verifies payment entirely outbound.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

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

_API = "https://api.stripe.com/v1"


@dataclass
class CheckoutSession:
    id: str
    url: str
    status: str            # open | complete | expired
    payment_status: str    # unpaid | paid | no_payment_required
    amount_total: Decimal | None
    currency: str | None
    payment_intent: str | None
    raw: dict

    @property
    def is_paid(self) -> bool:
        return self.status == "complete" and self.payment_status == "paid"

    @property
    def is_expired(self) -> bool:
        return self.status == "expired"


class StripeError(RuntimeError):
    pass


class StripeGateway:
    key = "stripe"
    label = "Card / Apple Pay / Cash App"

    def __init__(
        self,
        secret_key: str,
        *,
        success_url: str = "https://t.me",
        cancel_url: str = "https://t.me",
        payment_methods: list[str] | None = None,
        timeout: int = 30,
    ) -> None:
        self.secret_key = secret_key
        self.success_url = success_url
        self.cancel_url = cancel_url
        self.payment_methods = payment_methods or ["card", "cashapp"]
        self.live = secret_key.startswith("sk_live")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    async def create_session(
        self,
        *,
        product_name: str,
        amount: Decimal,
        currency: str,
        order_id: int,
        chat_id: int,
    ) -> CheckoutSession:
        """Create a Checkout Session for a single unit of `product_name`."""
        cents = int((Decimal(str(amount)) * 100).quantize(Decimal("1")))
        # Stripe wants form-encoded, bracketed nested params.
        data = [
            ("mode", "payment"),
            ("success_url", self.success_url),
            ("cancel_url", self.cancel_url),
            ("client_reference_id", str(order_id)),
            ("metadata[order_id]", str(order_id)),
            ("metadata[chat_id]", str(chat_id)),
            ("line_items[0][quantity]", "1"),
            ("line_items[0][price_data][currency]", currency.lower()),
            ("line_items[0][price_data][unit_amount]", str(cents)),
            ("line_items[0][price_data][product_data][name]", product_name),
        ]
        for i, pm in enumerate(self.payment_methods):
            data.append((f"payment_method_types[{i}]", pm))

        try:
            resp = await self._client.post(
                f"{_API}/checkout/sessions",
                headers=self._headers,
                content=urlencode(data),
            )
        except httpx.HTTPError as exc:
            raise StripeError(f"could not reach Stripe: {exc}") from exc
        if resp.status_code >= 400:
            raise StripeError(
                f"Stripe {resp.status_code}: {resp.text[:300]}"
            )
        return self._parse(resp.json())

    async def get_session(self, session_id: str) -> CheckoutSession:
        resp = await self._client.get(
            f"{_API}/checkout/sessions/{session_id}", headers=self._headers
        )
        if resp.status_code >= 400:
            raise StripeError(f"Stripe {resp.status_code}: {resp.text[:300]}")
        return self._parse(resp.json())

    # ── uniform gateway interface (see app/payments/gateway.py) ──────────
    async def create_checkout(
        self, *, product_name: str, amount, currency: str, order_id: int, chat_id: int
    ) -> CheckoutLink:
        s = await self.create_session(
            product_name=product_name, amount=amount, currency=currency,
            order_id=order_id, chat_id=chat_id,
        )
        return CheckoutLink(ref=s.id, url=s.url)

    async def poll(self, ref: str) -> PaymentState:
        try:
            s = await self.get_session(ref)
        except StripeError as exc:
            return PaymentState(ERROR, reason=str(exc))
        if s.is_paid:
            return PaymentState(
                PAID, amount=s.amount_total, currency=s.currency,
                txn_ref=s.payment_intent or s.id,
            )
        if s.is_expired:
            return PaymentState(EXPIRED, reason="checkout expired")
        return PaymentState(UNPAID)

    async def expire_session(self, session_id: str) -> None:
        try:
            await self._client.post(
                f"{_API}/checkout/sessions/{session_id}/expire",
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            log.warning("Stripe expire failed for %s: %s", session_id, exc)

    async def ping(self) -> tuple[bool, str]:
        """Validate the API key; returns (ok, detail)."""
        try:
            resp = await self._client.get(f"{_API}/account", headers=self._headers)
        except httpx.HTTPError as exc:
            return False, str(exc)
        if resp.status_code == 200:
            acct = resp.json()
            mode = "LIVE" if self.live else "test"
            return True, f"{acct.get('id','account')} ({mode})"
        return False, f"{resp.status_code}: {resp.text[:200]}"

    @staticmethod
    def _parse(d: dict) -> CheckoutSession:
        total = d.get("amount_total")
        return CheckoutSession(
            id=d.get("id", ""),
            url=d.get("url", ""),
            status=d.get("status", ""),
            payment_status=d.get("payment_status", ""),
            amount_total=(Decimal(total) / 100) if total is not None else None,
            currency=(d.get("currency") or "").upper() or None,
            payment_intent=d.get("payment_intent"),
            raw=d,
        )
