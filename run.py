#!/usr/bin/env python3
"""Entry point.

  python run.py            # start the bot
  python run.py --check    # preflight: verify every connection, then exit
"""
import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--check", "check", "doctor"):
        from app.doctor import main as check_main
        raise SystemExit(check_main())
    from app.bot import main as bot_main
    bot_main()


if __name__ == "__main__":
    main()
