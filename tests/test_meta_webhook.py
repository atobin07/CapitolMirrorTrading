"""Tests for the Instagram/Messenger webhook front-end.
Run: python tests/test_meta_webhook.py   (no server / aiohttp needed)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.meta_webhook import (  # noqa: E402
    handle_message,
    parse_events,
    verify_challenge,
    verify_signature,
)
from app.ratelimit import RateLimiter  # noqa: E402
from app.store import Store  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def test_verification_handshake():
    ch = verify_challenge(
        {"hub.mode": "subscribe", "hub.verify_token": "sekret", "hub.challenge": "12345"},
        "sekret")
    check(ch == "12345", "echoes challenge on matching token")
    check(verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "wrong",
                            "hub.challenge": "x"}, "sekret") is None, "rejects wrong token")


def test_signature():
    body = b'{"hello":"world"}'
    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    check(verify_signature("appsecret", body, sig) is True, "valid signature passes")
    check(verify_signature("appsecret", body, "sha256=deadbeef") is False, "bad signature fails")
    check(verify_signature("appsecret", body, None) is False, "missing header fails")
    check(verify_signature("", body, None) is True, "no secret configured → skip (dev)")


def test_parse_events():
    payload = {"object": "instagram", "entry": [{"messaging": [
        {"sender": {"id": "17841400000"}, "message": {"text": "hey do you have the red one?"}},
        {"sender": {"id": "17841400000"}, "message": {"text": "our own echo", "is_echo": True}},
        {"sender": {"id": "9"}, "message": {"attachments": [{"type": "image"}]}},  # no text
    ]}]}
    events = parse_events(payload)
    check(events == [("17841400000", "hey do you have the red one?")],
          "extracts text DMs, skips echoes + non-text")


class _LLM:
    async def chat(self, messages):
        # Echo that it saw the system prompt + user text (persona wiring).
        assert messages[0]["role"] == "system"
        return "**hey** — depends what you're after"


async def test_handle_message_engine():
    store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    cfg = SimpleNamespace(
        max_input_chars=1000, disclosure_enabled=True,
        disclosure_text="automated assistant for {business}.", business_name="Acme")
    rl = RateLimiter(max_per_min=5)

    out = await handle_message(cfg, store, _LLM(), "SYSTEM", rl, "17841400000", "hi")
    check(any("automated assistant for Acme" in m for m in out), "discloses on first DM")
    check(any("depends what you're after" in m for m in out), "returns the LLM reply")
    check(all("**" not in m for m in out), "markdown stripped from reply")
    check(len(store.get_history(17841400000)) == 2, "history saved for the IG user")

    # Disclosure only once.
    out2 = await handle_message(cfg, store, _LLM(), "SYSTEM", rl, "17841400000", "again")
    check(not any("automated assistant" in m for m in out2), "no repeat disclosure")

    # Rate limit kicks in and returns a single warning then silence.
    rl2 = RateLimiter(max_per_min=1)
    await handle_message(cfg, store, _LLM(), "SYSTEM", rl2, "5", "one")
    warned = await handle_message(cfg, store, _LLM(), "SYSTEM", rl2, "5", "two")
    silent = await handle_message(cfg, store, _LLM(), "SYSTEM", rl2, "5", "three")
    check(len(warned) == 1 and "sec" in warned[0], "warns once when over the limit")
    check(silent == [], "then stays silent under flood")
    store.close()


async def main():
    test_verification_handshake()
    test_signature()
    test_parse_events()
    await test_handle_message_engine()
    print(f"\nALL {_checks} META-WEBHOOK CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
