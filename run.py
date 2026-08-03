#!/usr/bin/env python3
"""Entry point.

  python run.py                          # start the bot (.env)
  python run.py --env clients/acme.env   # start a specific client's bot
  python run.py --check                  # preflight: verify every connection
  python run.py stock <id> <file>        # load digital inventory
  python run.py stock                    # show current inventory
  python run.py new-client <name>        # scaffold a new client config
  python run.py test                     # run every test suite (proof of work)

--env <file> works with any command and picks that client's config, so one
checkout of this repo can run many client bots side by side.
"""
import sys


def _load_env_flag(argv: list[str]) -> list[str]:
    """Pull an optional `--env <file>` out of argv and load it first."""
    if "--env" in argv:
        i = argv.index("--env")
        try:
            path = argv[i + 1]
        except IndexError:
            print("usage: --env <path-to-env-file>")
            raise SystemExit(2)
        from dotenv import load_dotenv
        if not load_dotenv(path, override=True):
            print(f"⚠️  env file not found or empty: {path}")
        del argv[i:i + 2]
    return argv


def main() -> None:
    argv = _load_env_flag(sys.argv[1:])
    arg = argv[0] if argv else ""

    if arg in ("--check", "check", "doctor"):
        from app.doctor import main as check_main
        raise SystemExit(check_main())
    if arg == "stock":
        from app.stock_cli import main as stock_main
        raise SystemExit(stock_main(argv[1:]))
    if arg in ("new-client", "newclient"):
        from app.new_client import main as nc_main
        raise SystemExit(nc_main(argv[1:]))
    if arg in ("instagram", "meta", "webhook"):
        import asyncio

        from app.config import Config
        from app.meta_webhook import run_server
        asyncio.run(run_server(Config.load()))
        return
    if arg in ("web", "store", "storefront"):
        import asyncio

        from app.config import Config
        from app.webstore import run_server
        asyncio.run(run_server(Config.load()))
        return
    if arg in ("test", "tests", "proof"):
        from tests.run_all import main as test_main
        raise SystemExit(test_main())
    from app.bot import main as bot_main
    bot_main()


if __name__ == "__main__":
    main()
