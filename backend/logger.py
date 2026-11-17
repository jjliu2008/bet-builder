"""Snapshot logger: appends each poll to a JSON-lines log that `engine.stale.replay` can read.

Two files per UTC day, both under LOG_DIR (default data/snapshots):

    <date>.jsonl           the contract log format in docs/api_contract.md: `book`, `spot`, and
                           `fair` records. Every record from one poll has the same `ts`.
    deribit-<date>.jsonl   one record per logged surface refresh, so a replay can rebuild the
                           fair-value surface:
                           {"type":"deribit","ts":...,"asset":"BTC","fields":[...],"rows":[[...], ...]}
                           `rows` holds the three fields `engine.build_surface` reads, for the
                           out-of-the-money options it uses (it discards the rest), so the rebuilt
                           surface is identical. `rows_total` is the count before that filter.

Size guard. Only Gemini books and Kalshi books inside the full-depth window are logged. The
logger measures the bytes each logged poll writes and, unless LOG_EVERY_N_POLLS is set, logs
every Nth poll with N chosen so a day stays under LOG_MAX_MB. Past that size it stops for the day.

Disk writes happen on a background thread. A write error is logged and counted; it never
reaches the poll loop.

Environment:
    LOG_DIR             where to write. Default data/snapshots.
    LOG_ENABLED         0 turns logging off. Default 1.
    LOG_EVERY_N_POLLS   log every Nth poll. Default 0 = choose N from the measured size.
    LOG_MAX_MB          the daily budget for both files together, in MB. Default 100.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from engine import Book, expiry_key, fair_above
from engine.books import iso, to_utc
from engine.fairvalue import fair_hidden
from engine.stale import book_to_record, iso_ms, record_to_book

log = logging.getLogger("backend.logger")
DERIBIT_FIELDS = ("instrument_name", "mark_iv", "underlying_price")
MB = 1_000_000
HEADROOM = 0.9            # aim below the budget, since poll sizes vary
_STOP = object()


@dataclass
class LogSettings:
    dir: str = "data/snapshots"
    enabled: bool = True
    every_n_polls: int = 0            # 0 = automatic
    max_mb: float = 100.0
    warm_s: float = 600.0             # how far back the startup reload goes

    @classmethod
    def from_env(cls) -> "LogSettings":
        d = cls()
        env = os.environ.get
        return cls(dir=env("LOG_DIR") or d.dir,
                   enabled=(env("LOG_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off"),
                   every_n_polls=int(env("LOG_EVERY_N_POLLS") or d.every_n_polls),
                   max_mb=float(env("LOG_MAX_MB") or d.max_mb))


def in_window(book: Book, window: Optional[Tuple[float, float]]) -> bool:
    """Return True if the book's strike (or bucket range) is inside the ($lo, $hi) window."""
    if window is None:
        return False
    lo = book.lo if book.lo is not None else book.hi
    hi = book.hi if book.hi is not None else book.lo
    if lo is None or hi is None:
        return False
    return hi >= window[0] and lo <= window[1]


def select_books(state, window_pct: float) -> List[Book]:
    """Pick the books worth logging: every Gemini book, and Kalshi books near spot.

    With no spot (Deribit has never answered) there is no window, so the Kalshi side falls back
    to the thresholds the matcher paired with a Gemini contract.
    """
    out = list(state.books["gemini"].values())
    kal = state.books["kalshi"]
    if state.spot:
        window = (state.spot * (1 - window_pct), state.spot * (1 + window_pct))
        out += [b for b in kal.values() if in_window(b, window)]
    elif state.match is not None:
        paired = {p.kalshi for e in state.expiries if e in state.match.expiries
                  for p in state.match.expiries[e].pairs}
        out += [b for cid, b in kal.items() if cid in paired]
    return out


def snapshot_lines(ts: datetime, books: Sequence[Book], spot: Optional[float], surface, asset: str = "BTC") -> List[str]:
    """Serialize one poll into contract-format JSON lines: books, then spot, then fair values.

    Fair values are written for each expiry whose ladder would show them, i.e. a surface exists
    and the expiry is at least 2 hours away.
    """
    dumps = lambda o: json.dumps(o, separators=(",", ":"))
    stamp = iso_ms(ts)
    lines = [dumps(book_to_record(b, ts)) for b in books]
    if spot:
        lines.append(dumps({"type": "spot", "ts": stamp, "asset": asset, "price": spot}))
    if surface is not None:
        strikes: Dict[str, set] = {}
        for b in books:
            if b.kind == "threshold":
                strikes.setdefault(expiry_key(b.expiry), set()).add(b.strike)
        for expiry in sorted(strikes):
            if fair_hidden(expiry, ts):
                continue
            try:
                fair = fair_above(asset, expiry, sorted(strikes[expiry]), surface=surface)
            except (ValueError, KeyError):
                continue
            lines += [dumps({"type": "fair", "ts": stamp, "asset": asset, "expiry": expiry,
                             "strike": k, "p_above": round(p, 6)}) for k, p in fair.items()]
    return lines


def used_by_surface(row: dict) -> bool:
    """Return False only for rows `engine.build_surface` is certain to discard.

    It keeps out-of-the-money options with a positive vol and forward: calls with K >= F and puts
    with K < F. A row whose name can't be parsed is kept, so nothing unknown is dropped here.
    """
    iv, fwd = row.get("mark_iv"), row.get("underlying_price")
    try:
        _, _, strike, cp = str(row.get("instrument_name", "")).split("-")
        strike = float(strike)
    except ValueError:
        return True
    if not iv or not fwd or iv <= 0 or fwd <= 0:
        return False
    return not ((cp == "C" and strike < fwd) or (cp == "P" and strike >= fwd))


def deribit_line(ts: datetime, rows: Sequence[dict], asset: str = "BTC") -> str:
    """Serialize the Deribit option rows a surface was built from, as one JSON line."""
    kept = [r for r in rows if used_by_surface(r)]
    return json.dumps({"type": "deribit", "ts": iso_ms(ts), "asset": asset, "fields": list(DERIBIT_FIELDS),
                       "rows_total": len(rows), "rows": [[r.get(f) for f in DERIBIT_FIELDS] for r in kept]},
                      separators=(",", ":"))


def deribit_rows(record: dict) -> List[dict]:
    """Turn a `deribit` log record back into the row dicts `engine.build_surface` takes."""
    fields = record.get("fields") or DERIBIT_FIELDS
    return [dict(zip(fields, r)) for r in record.get("rows", [])]


def tail_records(path: str, since: datetime) -> Iterator[dict]:
    """Yield the records in `path` with ts >= since, oldest first, reading only the file's tail.

    Lines that are not valid JSON (for example a line cut short by a crash) are skipped.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    block = 1 << 20
    with open(path, "rb") as f:
        while True:
            start = max(0, size - block)
            f.seek(start)
            lines = f.read().split(b"\n")
            if start > 0:
                lines = lines[1:]                          # the first line may be cut in half
            first = next((_ts(l) for l in lines if _ts(l) is not None), None)
            if start == 0 or (first is not None and first < since):
                break
            block *= 4
    for raw in lines:
        try:
            rec = json.loads(raw)
            if to_utc(rec["ts"]) >= since:
                yield rec
        except (ValueError, KeyError, TypeError):
            continue


def _ts(raw: bytes) -> Optional[datetime]:
    try:
        return to_utc(json.loads(raw)["ts"])
    except (ValueError, KeyError, TypeError):
        return None


class SnapshotLogger:
    """Writes polls to disk off the event loop and keeps the numbers /health reports.

    Args:
        settings: directory, cadence, and budget.
        poll_interval_s: seconds between polls, for the per-day estimate.
        window_pct: the full-depth window, as a fraction of spot.
        background: False writes inline, which tests use.
    """

    def __init__(self, settings: Optional[LogSettings] = None, poll_interval_s: float = 30.0,
                 window_pct: float = 0.03, background: bool = True):
        self.settings = settings or LogSettings()
        self.poll_interval_s = poll_interval_s
        self.window_pct = window_pct
        self.background = background
        self.every_n = max(1, self.settings.every_n_polls)
        self.auto = self.settings.every_n_polls <= 0
        self._since = 10 ** 9                              # polls since the last one logged; the first is logged
        self._sizes: deque = deque(maxlen=20)              # bytes of recent logged polls
        self._rows_at: Optional[datetime] = None           # surface time last written
        self._day: Optional[str] = None
        self._queue: "queue.Queue" = queue.Queue(maxsize=20)
        self._thread: Optional[threading.Thread] = None
        # stats
        self.records_written = 0
        self.polls_logged = 0
        self.polls_skipped_cap = 0
        self.bytes_last_poll = 0
        self.bytes_today = 0
        self.capped = False
        self.last_write: Optional[datetime] = None
        self.write_errors = 0
        self.last_error: Optional[str] = None
        self.dropped = 0
        self.warm_start: Optional[dict] = None
        self.path: Optional[str] = None
        self.deribit_path: Optional[str] = None

    # -- paths
    def paths(self, ts: datetime) -> Tuple[str, str]:
        day = to_utc(ts).strftime("%Y-%m-%d")
        return (os.path.join(self.settings.dir, f"{day}.jsonl"),
                os.path.join(self.settings.dir, f"deribit-{day}.jsonl"))

    # -- called by the poller, on the event loop
    def on_poll(self, state) -> bool:
        """Offer a finished poll. Returns True if it was queued for writing. Never raises or blocks."""
        try:
            self._since += 1
            if self._since < self.every_n:
                return False
            self._since = 0
            ts = state.last_poll or state.now()
            rows = None
            surface_at = getattr(state, "surface_at", None)
            if state.deribit_rows and surface_at != self._rows_at:
                rows, self._rows_at = state.deribit_rows, surface_at
            job = (ts, select_books(state, self.window_pct), state.spot, state.surface, rows, state.asset)
            if not self.background:
                self._write(*job)
                return True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="snapshot-logger", daemon=True)
                self._thread.start()
            try:
                self._queue.put_nowait(job)
            except queue.Full:                              # the disk is not keeping up: drop, don't wait
                self.dropped += 1
                self.last_error = "write queue full; poll dropped"
                return False
            return True
        except Exception as e:
            self._fail(e)
            return False

    # -- writer thread
    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is _STOP:
                    return
                self._write(*job)
            finally:
                self._queue.task_done()

    def _write(self, ts, books, spot, surface, rows, asset) -> None:
        try:
            path, dpath = self.paths(ts)
            day = os.path.basename(path)
            if day != self._day:                            # first write, or a new UTC day
                self._day, self.capped = day, False
                self.bytes_today = sum(os.path.getsize(p) for p in (path, dpath) if os.path.exists(p))
            self.path, self.deribit_path = path, dpath
            lines = snapshot_lines(ts, books, spot, surface, asset)
            body = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
            dbody = (deribit_line(ts, rows, asset) + "\n").encode("utf-8") if rows else b""
            total = len(body) + len(dbody)
            if self.bytes_today + total > self.settings.max_mb * MB:
                self.capped = True
                self.polls_skipped_cap += 1
                return
            os.makedirs(self.settings.dir, exist_ok=True)
            for p, data in ((path, body), (dpath, dbody)):
                if data:
                    with open(p, "ab") as f:
                        f.write(data)
            self.records_written += len(lines) + bool(dbody)
            self.polls_logged += 1
            self.bytes_last_poll = total
            self.bytes_today += total
            self.last_write = ts
            self._sizes.append(total)
            if self.auto:
                self.every_n = self._auto_n()
        except Exception as e:
            self._fail(e)

    def _fail(self, e: Exception) -> None:
        self.write_errors += 1
        self.last_error = f"{type(e).__name__}: {e}"[:300]
        log.error("snapshot log write failed: %s", self.last_error)

    # -- size guard
    def _avg_bytes(self) -> float:
        return sum(self._sizes) / len(self._sizes) if self._sizes else 0.0

    def _auto_n(self) -> int:
        """Return the smallest N for which logging every Nth poll stays under the daily budget."""
        per_day_every_poll = self._avg_bytes() * 86400.0 / self.poll_interval_s
        # The 0.02 keeps N from flipping when the size sits right on a boundary.
        return max(1, math.ceil(per_day_every_poll / (self.settings.max_mb * MB * HEADROOM) - 0.02))

    def est_mb_per_day(self) -> Optional[float]:
        if not self._sizes:
            return None
        return round(self._avg_bytes() * 86400.0 / (self.poll_interval_s * self.every_n) / MB, 1)

    # -- startup reload
    def warm(self, state) -> dict:
        """Reload the last `warm_s` seconds of book mids and spot from the log into `state.history`.

        This makes the frozen-quote rule usable right after a restart. Never raises.
        """
        now = state.now()
        since = now - timedelta(seconds=self.settings.warm_s)
        n_books = n_spot = 0
        first = None
        try:
            paths = [self.paths(since)[0], self.paths(now)[0]]
            for path in dict.fromkeys(paths):               # yesterday's file first, just after midnight
                for rec in tail_records(path, since):
                    ts = to_utc(rec["ts"])
                    if ts > now:
                        continue
                    kind = rec.get("type", "book")
                    if kind == "book":
                        state.history.record_book(record_to_book(rec), ts)
                        n_books += 1
                    elif kind == "spot":
                        state.history.record_spot(rec.get("asset", state.asset), float(rec["price"]), ts)
                        n_spot += 1
                    else:
                        continue
                    first = first or ts
        except Exception as e:
            self._fail(e)
        self.warm_start = {"book_records": n_books, "spot_records": n_spot, "from": iso(first) if first else None}
        return self.warm_start

    # -- lifecycle
    def flush(self) -> None:
        """Wait until everything queued has been written."""
        if self._thread is not None:
            self._queue.join()

    def close(self) -> None:
        if self._thread is not None:
            self._queue.put(_STOP)
            self._thread.join(timeout=10)
            self._thread = None

    def stats(self) -> dict:
        """The `snapshot_log` field of /health."""
        return {"enabled": True, "path": self.path, "deribit_path": self.deribit_path,
                "every_n_polls": self.every_n, "every_n_mode": "auto" if self.auto else "fixed",
                "polls_logged": self.polls_logged, "records_written": self.records_written,
                "bytes_last_poll": self.bytes_last_poll, "bytes_today": self.bytes_today,
                "est_mb_per_day": self.est_mb_per_day(), "max_mb": self.settings.max_mb,
                "capped": self.capped, "polls_skipped_cap": self.polls_skipped_cap,
                "last_write": iso(self.last_write) if self.last_write else None,
                "write_errors": self.write_errors, "dropped": self.dropped, "last_error": self.last_error,
                "warm_start": self.warm_start}
