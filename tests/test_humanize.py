"""Tests for the human-like chat layer. Run: python tests/test_humanize.py"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import humanize  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def test_strip_markdown():
    out = humanize.strip_markdown("**Hey** there, check `this` out")
    check("**" not in out and "`" not in out, "removes bold/code markers")
    check(out == "Hey there, check this out", "keeps the words")

    bullets = humanize.strip_markdown("- one\n- two\n1. three")
    check("-" not in bullets and "1." not in bullets, "strips bullets/numbers")

    heading = humanize.strip_markdown("# Big title\nbody")
    check(heading.startswith("Big title"), "strips heading marker")


def test_split_bubbles():
    # Paragraph breaks become separate bubbles.
    parts = humanize.split_bubbles("first thought\n\nsecond thought", 3)
    check(parts == ["first thought", "second thought"], "splits on blank lines")

    # One long blob splits by sentence.
    blob = "Hey there. I can help with that. What size do you need?"
    parts = humanize.split_bubbles(blob, 3)
    check(len(parts) >= 2, "long blob splits into multiple bubbles")

    # Short single line stays one bubble.
    parts = humanize.split_bubbles("sounds good!", 3)
    check(parts == ["sounds good!"], "short line stays one bubble")

    # Respects the max-bubbles cap.
    many = "a. b. c. d. e. f."
    parts = humanize.split_bubbles(many, 2)
    check(len(parts) <= 2, "never exceeds max_bubbles")


def test_typing_delay():
    d_short = humanize.typing_delay("hi", 18)
    d_long = humanize.typing_delay("x" * 300, 18)
    check(d_long > d_short, "longer text = longer delay")
    check(0.5 <= d_short <= 4.6 and d_long <= 4.6, "delays stay in human bounds")


class _Bot:
    def __init__(self):
        self.sent = []
        self.actions = 0

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

    async def send_chat_action(self, chat_id, action):
        self.actions += 1


async def test_deliver_bubbles_and_plain():
    # Humanized: multiple bubbles + typing actions, markdown stripped.
    cfg = SimpleNamespace(humanize=True, typing_cps=1000, max_bubbles=3)
    bot = _Bot()
    await humanize.deliver(bot, 1, "**Hi!**\n\nWhat are you after?", cfg)
    check(bot.sent == ["Hi!", "What are you after?"], "delivers clean bubbles")
    check(bot.actions >= 2, "shows a typing action per bubble")

    # Disabled: one plain message, no formatting left over.
    cfg2 = SimpleNamespace(humanize=False, typing_cps=18, max_bubbles=3)
    bot2 = _Bot()
    await humanize.deliver(bot2, 1, "**Hello** world", cfg2)
    check(bot2.sent == ["Hello world"], "plain mode sends one clean message")


def test_persona_prompt():
    from app.sales import build_system_prompt
    sp = build_system_prompt(
        "Acme", {"currency": "USD", "products": [{"id": "x", "name": "X", "price": 5}]},
        "", persona_name="Alex", persona_style="you love sneakers",
    )
    check("You are Alex" in sp, "persona name in prompt")
    check("NO markdown" in sp, "texting rules in prompt")
    check("you love sneakers" in sp, "custom style in prompt")
    # Reluctant / skeptical / make-them-chase posture.
    low = sp.lower()
    check("don't need this sale" in low, "not-desperate posture in prompt")
    check("skeptical" in low, "skeptical posture in prompt")
    check("let them chase" in low, "make-them-chase posture in prompt")
    check("never sound salesy" in low, "not-salesy rule in prompt")


async def main():
    test_strip_markdown()
    test_split_bubbles()
    test_typing_delay()
    await test_deliver_bubbles_and_plain()
    test_persona_prompt()
    print(f"\nALL {_checks} HUMANIZE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
