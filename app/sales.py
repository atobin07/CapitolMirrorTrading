"""Sales persona: builds the system prompt and detects buying intent."""
from __future__ import annotations

import re


def format_price(amount, currency: str) -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper(), "")
    if symbol:
        return f"{symbol}{amount}"
    return f"{amount} {currency}"


def build_system_prompt(
    business_name: str,
    catalog: dict,
    checkout_url: str,
    payment_methods: list[str] | None = None,
) -> str:
    currency = catalog.get("currency", "USD")
    lines: list[str] = []

    lines.append(
        f"You are a friendly, sharp sales assistant for {business_name}, "
        "chatting with a customer on Telegram. Your goal is to help the "
        "customer find the right product and confidently complete a purchase."
    )
    lines.append("")
    lines.append("PRODUCTS YOU SELL:")
    for p in catalog.get("products", []):
        price = format_price(p.get("price", "?"), currency)
        line = f"- {p['name']} ({price}): {p.get('summary', '')}"
        if p.get("details"):
            line += f" {p['details']}"
        lines.append(line)

    faq = catalog.get("faq", [])
    if faq:
        lines.append("")
        lines.append("FREQUENTLY ASKED QUESTIONS:")
        for item in faq:
            lines.append(f"- Q: {item['q']}\n  A: {item['a']}")

    lines.append("")
    lines.append("HOW TO SELL:")
    lines.append(
        "- Be warm, concise, and genuinely helpful. Keep replies short "
        "(1-4 sentences) — this is a chat, not an essay."
    )
    lines.append(
        "- Ask one question at a time to understand what the customer needs, "
        "then recommend the single best-fit product."
    )
    lines.append(
        "- Handle objections honestly. Emphasize real value, not hype. "
        "Never invent features, prices, discounts, or guarantees that are not "
        "listed above. If you don't know something, say a human will follow up."
    )
    lines.append(
        "- When the customer shows they want to buy, enthusiastically confirm "
        "their choice and move them to checkout."
    )
    if payment_methods:
        methods = ", ".join(payment_methods)
        lines.append(
            f"- To complete a purchase, tell the customer to tap /buy — they can "
            f"pay by {methods}, and the payment is confirmed automatically. Do NOT "
            "ask for payment IDs, card numbers, or confirm payments yourself; the "
            "/buy flow handles all of that securely."
        )
    elif checkout_url:
        lines.append(
            f"- To complete a purchase, share this checkout link: {checkout_url}"
        )
    else:
        lines.append(
            "- To complete a purchase, tell them you're connecting them with the "
            "team to finalize, and ask for the best way to reach them (or confirm "
            "their Telegram handle works)."
        )
    lines.append(
        "- Never ask for full card numbers, passwords, or sensitive personal "
        "data in chat."
    )
    lines.append(
        "- Stay on topic. If asked something unrelated to the products, gently "
        "steer back to how you can help them buy."
    )
    return "\n".join(lines)


# Phrases that suggest the customer is ready to buy — used to notify the seller.
_BUY_SIGNALS = [
    r"\bi'?ll take\b",
    r"\bi want to (buy|purchase|order)\b",
    r"\bhow do i (pay|buy|order|sign up)\b",
    r"\bready to (buy|purchase|order|pay)\b",
    r"\bsign me up\b",
    r"\blet'?s do it\b",
    r"\bi'?m (in|sold)\b",
    r"\bwhere do i pay\b",
    r"\bsend (me )?the (link|invoice|payment)\b",
    r"\bcheckout\b",
    r"\bi'?ll go with\b",
]
_BUY_RE = re.compile("|".join(_BUY_SIGNALS), re.IGNORECASE)


def detect_buying_signal(text: str) -> bool:
    return bool(_BUY_RE.search(text or ""))
