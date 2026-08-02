"""Builds the set of active payment verifiers from configuration."""
from __future__ import annotations

import logging

from app.payments.base import PaymentVerifier
from app.payments.manual import ManualVerifier
from app.payments.paypal import PayPalVerifier
from app.payments.square import SquareApplePayVerifier, SquareCashAppVerifier

log = logging.getLogger(__name__)


def build_verifiers(cfg) -> dict[str, PaymentVerifier]:
    """Return {key: verifier} for every provider the seller enabled.

    `cfg` is the app Config. Only providers listed in cfg.payment_providers are
    built, and each falls back to manual review if its API isn't configured.
    """
    verifiers: dict[str, PaymentVerifier] = {}

    for key in cfg.payment_providers:
        key = key.strip().lower()
        if not key:
            continue

        if key == "paypal":
            if cfg.paypal_client_id and cfg.paypal_secret:
                verifiers[key] = PayPalVerifier(
                    cfg.paypal_client_id,
                    cfg.paypal_secret,
                    env=cfg.paypal_env,
                    paypal_me=cfg.paypal_me,
                )
                log.info("PayPal: API verification enabled (%s).", cfg.paypal_env)
            else:
                verifiers[key] = ManualVerifier(
                    "paypal", "PayPal", handle=cfg.paypal_me,
                    note="_(manual review — add PayPal API keys for auto-verify)_",
                )
                log.warning("PayPal: no API keys set — falling back to manual review.")

        elif key == "cashapp":
            if cfg.square_access_token:
                verifiers[key] = SquareCashAppVerifier(
                    cfg.square_access_token,
                    env=cfg.square_env,
                    cashtag=cfg.cashapp_cashtag,
                )
                log.info("Cash App: Square API verification enabled (%s).", cfg.square_env)
            else:
                verifiers[key] = ManualVerifier(
                    "cashapp", "Cash App", handle=cfg.cashapp_cashtag,
                    note="_(P2P Cash App has no public API — payments are "
                         "human-verified)_",
                )
                log.info("Cash App: manual review (no Square token set).")

        elif key == "applepay":
            if cfg.square_access_token:
                verifiers[key] = SquareApplePayVerifier(
                    cfg.square_access_token,
                    env=cfg.square_env,
                    pay_link=cfg.applepay_link or cfg.checkout_url,
                )
                log.info("Apple Pay: Square API verification enabled (%s).", cfg.square_env)
            else:
                verifiers[key] = ManualVerifier(
                    "applepay", "Apple Pay",
                    handle=cfg.applepay_link or cfg.checkout_url,
                    note="_(Apple Pay needs a processor like Square/Stripe to "
                         "auto-verify — currently human-verified)_",
                )
                log.warning(
                    "Apple Pay: no Square token — falling back to manual review. "
                    "Apple Pay requires a payment processor (Square/Stripe)."
                )

        elif key == "venmo":
            # Venmo has no public verification API for personal accounts.
            verifiers[key] = ManualVerifier(
                "venmo", "Venmo", handle=cfg.venmo_handle,
                note="_(Venmo has no public API — payments are human-verified)_",
            )
            log.info("Venmo: manual review (no public verification API).")

        else:
            log.warning("Unknown payment provider '%s' — skipping.", key)

    return verifiers
