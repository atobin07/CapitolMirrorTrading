"""Tests for deep-link attribution. Run: python tests/test_attribution.py"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import Store  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def test_source_first_touch():
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    check(s.get_source(1) is None, "no source before any link")
    s.set_source(1, "maya")
    check(s.get_source(1) == "maya", "records the deep-link source")
    # First-touch wins — a later /start with a different tag doesn't overwrite.
    s.set_source(1, "someone-else")
    check(s.get_source(1) == "maya", "first-touch attribution is kept")
    # Independent per chat.
    s.set_source(2, "nova")
    check(s.get_source(2) == "nova" and s.get_source(1) == "maya", "per-chat sources")
    # Survives a conversation reset (attribution persists).
    s.reset(1)
    check(s.get_source(1) == "maya", "source persists across /start reset")
    s.close()


def main():
    test_source_first_touch()
    print(f"\nALL {_checks} ATTRIBUTION CHECKS PASSED")


if __name__ == "__main__":
    main()
