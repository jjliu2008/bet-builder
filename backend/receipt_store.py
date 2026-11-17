"""Persistent store for route receipts and hourly snapshot anchors.

`ReceiptStore` is a MutableMapping {route_id: {"request", "route", "receipt"}} — the shape
`backend.receipts.make_router` reads and writes — backed by:
    - Postgres when DATABASE_URL is set (psycopg 3), else
    - SQLite at RECEIPTS_DB (default data/receipts.db; data/ is gitignored).
Values are stored as JSON text. The connection is opened lazily on first use, so importing the
app never creates a database. Writes replace the whole record (mutating a returned dict in place
does not persist; assign it back).

The same database keeps snapshot anchors (see backend/logger.py): one row per UTC hour with the
Merkle root, line count, and the Solana signature.

Deploy note: most hosts wipe local files on redeploy. In production put RECEIPTS_DB on a
persistent disk/volume, or set DATABASE_URL to a hosted Postgres.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import MutableMapping
from datetime import datetime, timezone
from typing import Iterator, List, Optional

DEFAULT_DB = os.path.join("data", "receipts.db")

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS receipts (
        route_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        has_signature INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        data TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS anchors (
        hour TEXT PRIMARY KEY,
        root TEXT NOT NULL,
        count INTEGER NOT NULL,
        signature TEXT,
        explorer_url TEXT,
        created_at TEXT NOT NULL)""",
]


class ReceiptStore(MutableMapping):
    """Dict-like receipt store on SQLite or Postgres.

    Args:
        db_path: SQLite file (default env RECEIPTS_DB, else data/receipts.db).
        database_url: Postgres URL (default env DATABASE_URL). Takes precedence over db_path.
    """

    def __init__(self, db_path: Optional[str] = None, database_url: Optional[str] = None):
        self.database_url = database_url if database_url is not None else (os.environ.get("DATABASE_URL") or None)
        self.db_path = db_path or os.environ.get("RECEIPTS_DB") or DEFAULT_DB
        self._lock = threading.RLock()
        self._conn = None

    # ---- connection ---------------------------------------------------------------------
    @property
    def backend(self) -> str:
        """'postgres' or 'sqlite'."""
        return "postgres" if self.database_url else "sqlite"

    @property
    def _ph(self) -> str:
        return "%s" if self.database_url else "?"

    def _connect(self):
        if self._conn is not None:
            return self._conn
        if self.database_url:
            import psycopg
            conn = psycopg.connect(self.database_url, autocommit=True)
        else:
            d = os.path.dirname(os.path.abspath(self.db_path))
            os.makedirs(d, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
        for stmt in _SCHEMA:
            conn.execute(stmt)
        self._conn = conn
        return conn

    def _exec(self, sql: str, params: tuple = (), fetch: bool = False):
        sql = sql.replace("?", self._ph)
        with self._lock:
            cur = self._connect().execute(sql, params)
            return cur.fetchall() if fetch else None

    def close(self) -> None:
        """Close the connection (it reopens on next use)."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---- MutableMapping -----------------------------------------------------------------
    def __getitem__(self, route_id: str) -> dict:
        rows = self._exec("SELECT data FROM receipts WHERE route_id = ?", (route_id,), fetch=True)
        if not rows:
            raise KeyError(route_id)
        return json.loads(rows[0][0])

    def __setitem__(self, route_id: str, record: dict) -> None:
        rec = record.get("receipt") or {}
        self._exec(
            "INSERT INTO receipts (route_id, status, has_signature, created_at, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (route_id) DO UPDATE SET status = excluded.status, "
            "has_signature = excluded.has_signature, data = excluded.data",
            (route_id, rec.get("status", "pending"), 1 if rec.get("signature") else 0,
             rec.get("created_at") or datetime.now(timezone.utc).isoformat(), json.dumps(record)))

    def __delitem__(self, route_id: str) -> None:
        if route_id not in self:
            raise KeyError(route_id)
        self._exec("DELETE FROM receipts WHERE route_id = ?", (route_id,))

    def __contains__(self, route_id: object) -> bool:
        return bool(self._exec("SELECT 1 FROM receipts WHERE route_id = ?", (route_id,), fetch=True))

    def __iter__(self) -> Iterator[str]:
        return iter([r[0] for r in self._exec("SELECT route_id FROM receipts ORDER BY created_at", fetch=True)])

    def __len__(self) -> int:
        return int(self._exec("SELECT COUNT(*) FROM receipts", fetch=True)[0][0])

    # ---- queries ------------------------------------------------------------------------
    def pending_ids(self) -> List[str]:
        """Route ids whose receipt is still 'pending' with no signature (candidates for retry)."""
        rows = self._exec("SELECT route_id FROM receipts WHERE status = ? AND has_signature = 0 "
                          "ORDER BY created_at", ("pending",), fetch=True)
        return [r[0] for r in rows]

    # ---- snapshot anchors ---------------------------------------------------------------
    def save_anchor(self, hour: str, root: str, count: int, signature: Optional[str],
                    explorer_url: Optional[str]) -> None:
        """Record (or replace) the anchor for one UTC hour ('YYYYMMDDHH')."""
        self._exec(
            "INSERT INTO anchors (hour, root, count, signature, explorer_url, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (hour) DO UPDATE SET root = excluded.root, count = excluded.count, "
            "signature = excluded.signature, explorer_url = excluded.explorer_url",
            (hour, root, int(count), signature, explorer_url, datetime.now(timezone.utc).isoformat()))

    def has_anchor(self, hour: str) -> bool:
        """True if an anchor row exists for that hour."""
        return bool(self._exec("SELECT 1 FROM anchors WHERE hour = ?", (hour,), fetch=True))

    def anchors(self, limit: int = 48) -> List[dict]:
        """The latest `limit` anchors, newest hour first."""
        rows = self._exec("SELECT hour, root, count, signature, explorer_url, created_at FROM anchors "
                          "ORDER BY hour DESC LIMIT ?", (int(limit),), fetch=True)
        keys = ("hour", "root", "count", "signature", "explorer_url", "created_at")
        return [dict(zip(keys, r)) for r in rows]
