"""SQLite persistence for conversations and leads."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

# Cap how much history we keep/replay so the model prompt stays bounded.
MAX_TURNS = 12


class Store:
    def __init__(self, path: str) -> None:
        Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                chat_id     INTEGER PRIMARY KEY,
                username    TEXT,
                full_name   TEXT,
                history     TEXT NOT NULL DEFAULT '[]',
                updated_at  REAL
            );
            CREATE TABLE IF NOT EXISTS leads (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER,
                username    TEXT,
                full_name   TEXT,
                message     TEXT,
                created_at  REAL,
                notified    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS orders (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER,
                username     TEXT,
                full_name    TEXT,
                product_id   TEXT,
                product_name TEXT,
                amount       TEXT,
                currency     TEXT,
                provider     TEXT,
                payment_id   TEXT,
                state        TEXT NOT NULL DEFAULT 'pending',
                reason       TEXT,
                created_at   REAL,
                updated_at   REAL
            );
            -- Ledger of consumed payment IDs: prevents the same ID being reused.
            CREATE TABLE IF NOT EXISTS consumed_payments (
                provider    TEXT NOT NULL,
                payment_id  TEXT NOT NULL,
                order_id    INTEGER,
                created_at  REAL,
                PRIMARY KEY (provider, payment_id)
            );
            -- Digital inventory the bot delivers from. One row = one unique item
            -- (license key, account, link…). state: available|reserved|consumed
            CREATE TABLE IF NOT EXISTS stock (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id   TEXT NOT NULL,
                content      TEXT NOT NULL,
                state        TEXT NOT NULL DEFAULT 'available',
                order_id     INTEGER,
                reserved_at  REAL,
                consumed_at  REAL,
                created_at   REAL
            );
            CREATE INDEX IF NOT EXISTS idx_stock_lookup
                ON stock (product_id, state);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_unique
                ON stock (product_id, content);
            -- Tracks which chats have already seen the bot-disclosure notice.
            CREATE TABLE IF NOT EXISTS disclosures (
                chat_id    INTEGER PRIMARY KEY,
                shown_at   REAL
            );
            """
        )
        self._conn.commit()

    # ── conversations ────────────────────────────────────────────────────
    def get_history(self, chat_id: int) -> list[dict]:
        row = self._conn.execute(
            "SELECT history FROM conversations WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row["history"])
        except (json.JSONDecodeError, TypeError):
            return []

    def save_history(
        self, chat_id: int, history: list[dict], username: str, full_name: str
    ) -> None:
        trimmed = history[-(MAX_TURNS * 2):]
        self._conn.execute(
            """
            INSERT INTO conversations (chat_id, username, full_name, history, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                history=excluded.history,
                updated_at=excluded.updated_at
            """,
            (chat_id, username, full_name, json.dumps(trimmed), time.time()),
        )
        self._conn.commit()

    def reset(self, chat_id: int) -> None:
        self._conn.execute(
            "DELETE FROM conversations WHERE chat_id = ?", (chat_id,)
        )
        # A fresh conversation should disclose again.
        self._conn.execute(
            "DELETE FROM disclosures WHERE chat_id = ?", (chat_id,)
        )
        self._conn.commit()

    # ── bot-disclosure tracking ──────────────────────────────────────────
    def needs_disclosure(self, chat_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM disclosures WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row is None

    def mark_disclosed(self, chat_id: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO disclosures (chat_id, shown_at) VALUES (?, ?)",
            (chat_id, time.time()),
        )
        self._conn.commit()

    # ── leads ────────────────────────────────────────────────────────────
    def add_lead(
        self, chat_id: int, username: str, full_name: str, message: str
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO leads (chat_id, username, full_name, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, username, full_name, message, time.time()),
        )
        self._conn.commit()
        return cur.lastrowid

    def recent_leads(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM leads ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

    # ── orders ───────────────────────────────────────────────────────────
    # States: pending → awaiting_id/awaiting_payment → (reviewing) →
    #         paid | paid_no_stock | failed | cancelled | expired
    _TERMINAL = ("paid", "paid_no_stock", "failed", "cancelled", "expired")

    def create_order(
        self, chat_id: int, username: str, full_name: str,
        product_id: str, product_name: str, amount: str, currency: str,
    ) -> int:
        now = time.time()
        # Cancel any other open order for this chat to avoid ambiguity.
        self._conn.execute(
            "UPDATE orders SET state='cancelled', updated_at=? "
            "WHERE chat_id=? AND state NOT IN ('paid','failed','cancelled')",
            (now, chat_id),
        )
        cur = self._conn.execute(
            """
            INSERT INTO orders
                (chat_id, username, full_name, product_id, product_name,
                 amount, currency, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (chat_id, username, full_name, product_id, product_name,
             amount, currency, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM orders WHERE id=?", (order_id,)
        ).fetchone()

    def active_order_for_chat(self, chat_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM orders WHERE chat_id=? "
            "AND state NOT IN ('paid','failed','cancelled') "
            "ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()

    def set_order_state(
        self, order_id: int, state: str, *,
        provider: str | None = None, payment_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        fields = ["state=?", "updated_at=?"]
        params: list = [state, time.time()]
        if provider is not None:
            fields.append("provider=?"); params.append(provider)
        if payment_id is not None:
            fields.append("payment_id=?"); params.append(payment_id)
        if reason is not None:
            fields.append("reason=?"); params.append(reason)
        params.append(order_id)
        self._conn.execute(
            f"UPDATE orders SET {', '.join(fields)} WHERE id=?", params
        )
        self._conn.commit()

    def recent_orders(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def orders_awaiting_payment(self) -> list[sqlite3.Row]:
        """Open Stripe orders the poller must reconcile against Stripe."""
        return self._conn.execute(
            "SELECT * FROM orders WHERE state='awaiting_payment' "
            "AND payment_id IS NOT NULL ORDER BY created_at ASC"
        ).fetchall()

    # ── digital inventory (stock) ────────────────────────────────────────
    def add_stock(self, product_id: str, items: list[str]) -> int:
        """Bulk-add unique deliverables. Duplicates are ignored. Returns #added."""
        now = time.time()
        added = 0
        for raw in items:
            content = raw.strip()
            if not content:
                continue
            try:
                self._conn.execute(
                    "INSERT INTO stock (product_id, content, state, created_at) "
                    "VALUES (?, ?, 'available', ?)",
                    (product_id, content, now),
                )
                added += 1
            except sqlite3.IntegrityError:
                pass  # already stocked
        self._conn.commit()
        return added

    def available_count(self, product_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM stock WHERE product_id=? AND state='available'",
            (product_id,),
        ).fetchone()
        return row["n"] if row else 0

    def stock_summary(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT product_id, "
            "SUM(state='available') AS available, "
            "SUM(state='reserved') AS reserved, "
            "SUM(state='consumed') AS consumed "
            "FROM stock GROUP BY product_id ORDER BY product_id"
        ).fetchall()

    def reserve_stock(self, product_id: str, order_id: int) -> str | None:
        """Atomically hold one available item for an order. None if out of stock."""
        row = self._conn.execute(
            "UPDATE stock SET state='reserved', order_id=?, reserved_at=? "
            "WHERE id = (SELECT id FROM stock WHERE product_id=? AND state='available' "
            "            ORDER BY id LIMIT 1) "
            "RETURNING content",
            (order_id, time.time(), product_id),
        ).fetchone()
        self._conn.commit()
        return row["content"] if row else None

    def consume_reserved_stock(self, order_id: int) -> str | None:
        """Mark this order's reserved item consumed and return its content."""
        row = self._conn.execute(
            "UPDATE stock SET state='consumed', consumed_at=? "
            "WHERE order_id=? AND state='reserved' RETURNING content",
            (time.time(), order_id),
        ).fetchone()
        self._conn.commit()
        return row["content"] if row else None

    def release_reservation(self, order_id: int) -> None:
        """Return an order's reserved item to the available pool."""
        self._conn.execute(
            "UPDATE stock SET state='available', order_id=NULL, reserved_at=NULL "
            "WHERE order_id=? AND state='reserved'",
            (order_id,),
        )
        self._conn.commit()

    def release_stale_reservations(self) -> int:
        """On startup, free items reserved for orders that never completed."""
        cur = self._conn.execute(
            "UPDATE stock SET state='available', order_id=NULL, reserved_at=NULL "
            "WHERE state='reserved' AND order_id IN "
            "(SELECT id FROM orders WHERE state IN "
            " ('cancelled','failed','expired','pending'))"
        )
        self._conn.commit()
        return cur.rowcount

    # ── payment-id ledger (double-spend protection) ──────────────────────
    def is_payment_consumed(self, provider: str, payment_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM consumed_payments WHERE provider=? AND payment_id=?",
            (provider, payment_id),
        ).fetchone()
        return row is not None

    def consume_payment(self, provider: str, payment_id: str, order_id: int) -> bool:
        """Atomically claim a payment id. Returns False if already used."""
        try:
            self._conn.execute(
                "INSERT INTO consumed_payments (provider, payment_id, order_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (provider, payment_id, order_id, time.time()),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def close(self) -> None:
        self._conn.close()
