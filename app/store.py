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
    # States: pending → awaiting_id → (reviewing) → paid | failed | cancelled
    _TERMINAL = ("paid", "failed", "cancelled")

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
