#!/usr/bin/env python3
"""Entry point.

  python run.py                       # start the bot
  python run.py --check               # preflight: verify every connection
  python run.py stock <id> <file>     # load digital inventory (one item/line)
  python run.py stock                 # show current inventory
"""
import sys


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg in ("--check", "check", "doctor"):
        from app.doctor import main as check_main
        raise SystemExit(check_main())
    if arg == "stock":
        from app.stock_cli import main as stock_main
        raise SystemExit(stock_main(sys.argv[2:]))
    if arg in ("test", "tests", "proof"):
        from tests.run_all import main as test_main
        raise SystemExit(test_main())
    from app.bot import main as bot_main
    bot_main()


if __name__ == "__main__":
    main()
