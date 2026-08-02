"""Tests for compliance features. Run: python tests/test_compliance.py"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sales import build_system_prompt  # noqa: E402
from app.store import Store  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def test_disclosure_once():
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    check(s.needs_disclosure(5) is True, "new chat needs disclosure")
    s.mark_disclosed(5)
    check(s.needs_disclosure(5) is False, "disclosed chat doesn't repeat")
    s.mark_disclosed(5)  # idempotent
    check(s.needs_disclosure(5) is False, "marking twice is safe")

    # A fresh conversation (/start → reset) discloses again.
    s.reset(5)
    check(s.needs_disclosure(5) is True, "reset re-enables disclosure")

    # Independent per chat.
    check(s.needs_disclosure(6) is True, "different chat tracked separately")
    s.close()


def test_prompt_honesty():
    sp = build_system_prompt(
        "Acme", {"currency": "USD", "products": [{"id": "x", "name": "X", "price": 5}]},
        "", persona_name="Alex",
    )
    low = sp.lower()
    check("directly asks whether you're a bot" in low, "honest-if-asked rule present")
    check("automated assistant for acme" in low, "names the business in disclosure")
    check("never fabricate proof of being human" in low, "no-fake-human rule present")


async def _run_async():
    # Disclosure delivery + gating via the real bot helpers, with fakes.
    import app.bot as bot
    from types import SimpleNamespace

    class _Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, **kw):
            self.sent.append((chat_id, text))

    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    bot.cfg = SimpleNamespace(
        disclosure_enabled=True,
        disclosure_text="you're chatting with an automated assistant for {business}.",
        business_name="Acme",
    )
    bot.store = s
    ctx = SimpleNamespace(bot=_Bot())

    await bot._maybe_disclose(42, ctx)
    check(len(ctx.bot.sent) == 1, "discloses on first contact")
    check("Acme" in ctx.bot.sent[0][1], "{business} substituted in notice")
    await bot._maybe_disclose(42, ctx)
    check(len(ctx.bot.sent) == 1, "does not disclose twice")
    s.close()


def main():
    test_disclosure_once()
    test_prompt_honesty()
    import asyncio
    asyncio.run(_run_async())
    print(f"\nALL {_checks} COMPLIANCE CHECKS PASSED")


if __name__ == "__main__":
    main()
