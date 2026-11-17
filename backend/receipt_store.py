"""SQLite-backed store for route receipts, so /verify links survive a backend restart.

`SqliteReceiptStore` implements just the dict operations backend/receipts.py uses
(`in`, `[]`, `[]=`, `.get`). Values are JSON-serializable dicts.

Enable it by setting RECEIPTS_DB to a file path, for example RECEIPTS_DB=./receipts.db.
Without it, receipts stay in process memory as before.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Optional


class SqliteReceiptStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS receipts (route_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self._db.commit()

    def __contains__(self, route_id: object) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM receipts WHERE route_id=?", (route_id,)).fetchone() is not None

    def __getitem__(self, route_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT record FROM receipts WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise KeyError(route_id)
        return json.loads(row[0])

    def __setitem__(self, route_id: str, record: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO receipts (route_id, record) VALUES (?, ?)",
                             (route_id, json.dumps(record, default=str)))
            self._db.commit()

    def get(self, route_id: str, default: Any = None) -> Any:
        try:
            return self[route_id]
        except KeyError:
            return default

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]


def receipt_store_from_env() -> Optional[SqliteReceiptStore]:
    """Return a SQLite store when RECEIPTS_DB is set, else None (in-memory default)."""
    path = os.environ.get("RECEIPTS_DB", "").strip()
    return SqliteReceiptStore(os.path.expanduser(path)) if path else None
