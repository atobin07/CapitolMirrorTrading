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


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


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
    # LLM backend: "ollama" (local) or "openai" (any OpenAI-compatible API)
    llm_backend: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_max_tokens: int
    # Instagram / Meta DM webhook front-end
    meta_verify_token: str
    meta_app_secret: str
    meta_page_token: str
    meta_graph_version: str
    webhook_host: str
    webhook_port: int
    # Web storefront
    store_public_url: str
    stripe_webhook_secret: str
    business_name: str
    checkout_url: str
    database_path: str
    # Persona / human-like chat
    persona_name: str
    persona_style: str
    humanize: bool
    typing_cps: float
    max_bubbles: int
    # Compliance
    disclosure_enabled: bool
    disclosure_text: str
    refund_policy: str
    # Abuse protection (public bot)
    rate_limit_per_min: int
    max_input_chars: int
    support_contact: str
    terms_url: str
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
    # PayPal + Venmo autonomous checkout (Orders API)
    paypal_return_url: str
    paypal_cancel_url: str
    paypal_enable_venmo: bool
    catalog: dict = field(default_factory=dict)
    catalog_dir: str = "."

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def paypal_checkout_enabled(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_secret)

    @property
    def autonomous_checkout_enabled(self) -> bool:
        return self.stripe_enabled or self.paypal_checkout_enabled

    @property
    def payments_enabled(self) -> bool:
        return bool(self.payment_providers)

    @classmethod
    def load(cls) -> "Config":
        # Per-client catalog: CATALOG_PATH lets each bot use its own file.
        cat_path = _get("CATALOG_PATH", "catalog.json")
        catalog_path = Path(cat_path) if os.path.isabs(cat_path) else (ROOT / cat_path)
        catalog: dict = {}
        if catalog_path.exists():
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        # Product files (PDFs) resolve relative to the catalog's own directory.
        catalog_dir = str(catalog_path.resolve().parent)

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
            llm_backend=(_get("LLM_BACKEND", "ollama") or "ollama").lower(),
            llm_base_url=_get("LLM_BASE_URL").rstrip("/"),
            llm_api_key=_get("LLM_API_KEY"),
            llm_model=_get("LLM_MODEL"),
            llm_max_tokens=_get_int("LLM_MAX_TOKENS", 512),
            meta_verify_token=_get("META_VERIFY_TOKEN"),
            meta_app_secret=_get("META_APP_SECRET"),
            meta_page_token=_get("META_PAGE_ACCESS_TOKEN"),
            meta_graph_version=_get("META_GRAPH_VERSION", "v21.0"),
            webhook_host=_get("WEBHOOK_HOST", "0.0.0.0"),
            webhook_port=_get_int("WEBHOOK_PORT", 8080),
            store_public_url=_get("STORE_PUBLIC_URL", "http://localhost:8080").rstrip("/"),
            stripe_webhook_secret=_get("STRIPE_WEBHOOK_SECRET"),
            business_name=_get("BUSINESS_NAME", "Your Business"),
            checkout_url=_get("CHECKOUT_URL"),
            database_path=db_path,
            persona_name=_get("PERSONA_NAME"),
            persona_style=_get("PERSONA_STYLE"),
            humanize=_get_bool("HUMANIZE", True),
            typing_cps=_get_float("TYPING_CPS", 18.0),
            max_bubbles=_get_int("MAX_BUBBLES", 3),
            disclosure_enabled=_get_bool("DISCLOSURE_ENABLED", True),
            disclosure_text=_get(
                "DISCLOSURE_TEXT",
                "heads up — you're chatting with an automated assistant for "
                "{business}. i can still get you sorted. type /terms anytime for "
                "the refund policy.",
            ),
            refund_policy=_get(
                "REFUND_POLICY",
                "All sales are final once your item is delivered, unless it's "
                "defective or you never received it — in that case you get a full "
                "refund. Just reach out and we'll sort it out.",
            ),
            rate_limit_per_min=_get_int("RATE_LIMIT_PER_MIN", 15),
            max_input_chars=_get_int("MAX_INPUT_CHARS", 1000),
            support_contact=_get("SUPPORT_CONTACT"),
            terms_url=_get("TERMS_URL"),
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
            paypal_return_url=_get("PAYPAL_RETURN_URL", "https://t.me"),
            paypal_cancel_url=_get("PAYPAL_CANCEL_URL", "https://t.me"),
            paypal_enable_venmo=_get_bool("PAYPAL_ENABLE_VENMO", True),
            catalog=catalog,
            catalog_dir=catalog_dir,
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
