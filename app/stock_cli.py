"""Load digital inventory from a file.

  python run.py stock <product_id> <file.txt>   # add items (one per line)
  python run.py stock                            # show current inventory

Each non-empty line is one deliverable (license key, account, link…).
Duplicates already in stock are skipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.config import Config
from app.store import Store


def main(argv: list[str]) -> int:
    cfg = Config.load()
    store = Store(cfg.database_path)
    try:
        if not argv:
            rows = store.stock_summary()
            if not rows:
                print("No stock loaded yet.")
                return 0
            names = {str(p["id"]): p["name"] for p in cfg.catalog.get("products", [])}
            print("Inventory:")
            for r in rows:
                label = names.get(r["product_id"], r["product_id"])
                print(f"  {label} [{r['product_id']}]: "
                      f"{r['available'] or 0} available, "
                      f"{r['reserved'] or 0} reserved, {r['consumed'] or 0} sold")
            return 0

        if len(argv) < 2:
            print("usage: python run.py stock <product_id> <file.txt>")
            return 2

        product_id, file_path = argv[0], argv[1]
        known = {str(p["id"]) for p in cfg.catalog.get("products", [])}
        if known and product_id not in known:
            print(f"⚠️  '{product_id}' isn't a product id in catalog.json "
                  f"(known: {', '.join(sorted(known))}).")
            return 2
        path = Path(file_path)
        if not path.exists():
            print(f"File not found: {file_path}")
            return 2

        items = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        if not items:
            print("That file has no items.")
            return 2
        added = store.add_stock(product_id, items)
        skipped = len(items) - added
        print(f"✅ Added {added} item(s) to '{product_id}'"
              + (f" ({skipped} duplicate(s) skipped)" if skipped else "")
              + f". Now {store.available_count(product_id)} available.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
