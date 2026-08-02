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

    def close(self) -> None:
        self._conn.close()
