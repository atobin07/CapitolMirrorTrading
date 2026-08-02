"""Run every test suite and print a single proof-of-work summary.

  python run.py test        (or)  python tests/run_all.py
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SUITES = [
    ("Payments (PayPal/Square/manual, ledger)", "tests.test_payments"),
    ("Checkout (Stripe + inventory + delivery)", "tests.test_checkout"),
    ("Humanize (persona, bubbles, typing)", "tests.test_humanize"),
    ("Compliance (disclosure, terms, honesty)", "tests.test_compliance"),
    ("End-to-end (full purchase, real code)", "tests.test_e2e"),
]


def main() -> int:
    print("\n" + "#" * 60)
    print("#  PROOF OF WORK — running every test suite")
    print("#" * 60)
    failures = 0
    for label, module_name in SUITES:
        print(f"\n### {label}")
        try:
            mod = importlib.import_module(module_name)
            result = mod.main()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except AssertionError as exc:
            failures += 1
            print(f"  ❌ {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  ❌ error: {exc}")

    print("\n" + "#" * 60)
    if failures == 0:
        print("#  ✅ ALL SUITES PASSED")
    else:
        print(f"#  ❌ {failures} SUITE(S) FAILED")
    print("#" * 60 + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
