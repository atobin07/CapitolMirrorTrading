"""Tests for P2P pay links + unique-amount matching. Run: python tests/test_p2p.py"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import p2p  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def test_unique_amount():
    # Each order gets a distinct amount off the same base, for matching.
    a1 = p2p.unique_amount(20, 1)
    a2 = p2p.unique_amount(20, 2)
    check(a1 != a2, "different orders → different amounts")
    check(Decimal("20") < a1 <= Decimal("20.89"), "amount is base + small cents")
    check(a1.as_tuple().exponent == -2, "amount is 2 decimal places")
    # Same order id is stable (so re-showing the link doesn't change the amount).
    check(p2p.unique_amount(20, 7) == p2p.unique_amount(20, 7), "stable per order")


def test_links():
    ca = p2p.pay_link("cashapp", "$lumen", "20.05")
    check(ca == "https://cash.app/$lumen/20.05", "cash app link + amount")
    check(p2p.pay_link("cashapp", "lumen", "20.05") == "https://cash.app/$lumen/20.05",
          "cash app tolerates missing $")

    pp = p2p.pay_link("paypal", "lumenstore", "20.05")
    check(pp == "https://paypal.me/lumenstore/20.05", "paypal.me link + amount")

    vm = p2p.pay_link("venmo", "@lumen", "20.05", note="ORD0007")
    check("venmo.com/lumen" in vm and "amount=20.05" in vm and "note=ORD0007" in vm,
          "venmo link with amount + note")

    check(p2p.pay_link("cashapp", "", "5") is None, "no handle → no link")
    check(p2p.pay_link("applepay", "x", "5") is None, "unsupported app → None")


def test_instructions_and_note():
    note = p2p.order_note(42)
    check(note == "ORD0042", "note code from order id")
    txt = p2p.pay_instructions("cashapp", "$lumen", "20.05", note)
    check("exactly" in txt.lower() and "20.05" in txt and "$lumen" in txt,
          "instructions stress the exact amount + handle")


def main():
    test_unique_amount()
    test_links()
    test_instructions_and_note()
    print(f"\nALL {_checks} P2P CHECKS PASSED")


if __name__ == "__main__":
    main()
