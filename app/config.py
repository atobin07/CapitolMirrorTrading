"""Central configuration, loaded from environment / .env file."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name) or default)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name) or default)
    except ValueError:
        return default


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                pass
    return ids


@dataclass
class Config:
    telegram_token: str
    seller_chat_ids: list[int]
    ollama_host: str
    ollama_model: str
    ollama_temperature: float
    ollama_num_ctx: int
    ollama_timeout: int
    business_name: str
    checkout_url: str
    database_path: str
    # Payments
    payment_providers: list[str]
    paypal_env: str
    paypal_client_id: str
    paypal_secret: str
    paypal_me: str
    square_env: str
    square_access_token: str
    cashapp_cashtag: str
    applepay_link: str
    venmo_handle: str
    # Stripe (autonomous hosted checkout)
    stripe_secret_key: str
    stripe_success_url: str
    stripe_cancel_url: str
    stripe_payment_methods: list[str]
    stripe_poll_interval: int
    stripe_session_timeout: int
    catalog: dict = field(default_factory=dict)

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def payments_enabled(self) -> bool:
        return bool(self.payment_providers)

    @classmethod
    def load(cls) -> "Config":
        catalog_path = ROOT / "catalog.json"
        catalog: dict = {}
        if catalog_path.exists():
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

        db_path = _get("DATABASE_PATH", "data/leads.db")
        if not os.path.isabs(db_path):
            db_path = str(ROOT / db_path)

        return cls(
            telegram_token=_get("TELEGRAM_BOT_TOKEN"),
            seller_chat_ids=_parse_ids(_get("SELLER_CHAT_IDS")),
            ollama_host=_get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=_get("OLLAMA_MODEL", "llama3.1:8b"),
            ollama_temperature=_get_float("OLLAMA_TEMPERATURE", 0.6),
            ollama_num_ctx=_get_int("OLLAMA_NUM_CTX", 4096),
            ollama_timeout=_get_int("OLLAMA_TIMEOUT", 120),
            business_name=_get("BUSINESS_NAME", "Your Business"),
            checkout_url=_get("CHECKOUT_URL"),
            database_path=db_path,
            payment_providers=[
                p.strip().lower()
                for p in _get("PAYMENT_PROVIDERS").replace(";", ",").split(",")
                if p.strip()
            ],
            paypal_env=(_get("PAYPAL_ENV", "live") or "live").lower(),
            paypal_client_id=_get("PAYPAL_CLIENT_ID"),
            paypal_secret=_get("PAYPAL_SECRET"),
            paypal_me=_get("PAYPAL_ME"),
            square_env=(_get("SQUARE_ENV", "production") or "production").lower(),
            square_access_token=_get("SQUARE_ACCESS_TOKEN"),
            cashapp_cashtag=_get("CASHAPP_CASHTAG"),
            applepay_link=_get("APPLEPAY_LINK"),
            venmo_handle=_get("VENMO_HANDLE"),
            stripe_secret_key=_get("STRIPE_SECRET_KEY"),
            stripe_success_url=_get("STRIPE_SUCCESS_URL", "https://t.me"),
            stripe_cancel_url=_get("STRIPE_CANCEL_URL", "https://t.me"),
            stripe_payment_methods=[
                p.strip().lower()
                for p in (_get("STRIPE_PAYMENT_METHODS", "card,cashapp")
                          .replace(";", ",").split(","))
                if p.strip()
            ],
            stripe_poll_interval=_get_int("STRIPE_POLL_INTERVAL", 8),
            stripe_session_timeout=_get_int("STRIPE_SESSION_TIMEOUT", 1800),
            catalog=catalog,
        )

    def validate(self) -> list[str]:
        """Return a list of human-readable configuration problems."""
        problems: list[str] = []
        if not self.telegram_token or self.telegram_token.startswith("123456:ABC"):
            problems.append(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and "
                "put the token in your .env file."
            )
        if not self.catalog.get("products"):
            problems.append(
                "catalog.json has no products. Edit it with your real offers."
            )
        if not self.seller_chat_ids:
            problems.append(
                "SELLER_CHAT_IDS is empty — you won't get lead notifications. "
                "Set it to your Telegram numeric ID (from @userinfobot)."
            )
        return problems
