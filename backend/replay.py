"""Replay mode: serve the whole API from a recorded snapshot log, with no venue calls.

Set REPLAY_FILE to a contract-format log written by `backend.logger` (plain or .gz) and
REPLAY_DERIBIT_FILE to the matching Deribit file. The `Replayer` then stands in for the poller:
it walks the snapshots in order, loads each one into the shared `State`, and loops at the end.

The clock is virtual. `state.now()` returns the current snapshot's `ts`, so expiry checks, the
2-hour fair-value cutoff, and the stale rules all see the recorded moment, not today's date.

Environment:
    REPLAY_FILE           the snapshot log to serve. Setting it turns replay mode on.
    REPLAY_DERIBIT_FILE   the Deribit rows file, to rebuild the fair-value surface. Without it,
                          fair values are null and verdict_code is "unavailable".
    REPLAY_SPEED          playback speed. Default 1 (real time); 10 plays ten times faster.
"""
from __future__ import annotations

import asyncio
import bisect
import gzip
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from engine import Book, Surface, build_surface
from engine.books import iso, to_utc
from engine.stale import record_to_book

from .ingest.poller import VENUES, QuoteHistory, State, nearest_expiries
from .logger import deribit_rows
from .matcher import build_report


@dataclass
class Frame:
    """One recorded poll: every record that shares a timestamp."""
    ts: datetime
    books: List[Book] = field(default_factory=list)
    spot: Optional[float] = None


def read_records(path: str) -> Iterator[dict]:
    """Yield the JSON objects in a .jsonl or .jsonl.gz file. Blank, comment, and broken lines are skipped."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def load_frames(path: str, asset: str = "BTC") -> List[Frame]:
    """Group a snapshot log into frames by timestamp, oldest first. Unknown record types are ignored."""
    frames: Dict[datetime, Frame] = {}
    for rec in read_records(path):
        if not rec.get("ts"):
            continue
        kind = rec.get("type", "book")
        if kind not in ("book", "spot") or rec.get("asset", asset).upper() != asset.upper():
            continue
        ts = to_utc(rec["ts"])
        frame = frames.setdefault(ts, Frame(ts))
        if kind == "book":
            frame.books.append(record_to_book(rec))
        else:
            frame.spot = float(rec["price"])
    return [frames[ts] for ts in sorted(frames) if frames[ts].books]


def load_deribit(path: str, asset: str = "BTC") -> List[Tuple[datetime, List[dict]]]:
    """Read a Deribit rows file into [(ts, rows)], oldest first."""
    out = [(to_utc(r["ts"]), deribit_rows(r)) for r in read_records(path)
           if r.get("type") == "deribit" and r.get("ts") and r.get("asset", asset).upper() == asset.upper()]
    return sorted(out, key=lambda x: x[0])


class Replayer:
    """Feeds `State` from a recorded log. It has the poller's `run` / `stop` / `aclose` interface.

    Args:
        path: the snapshot log (.jsonl or .jsonl.gz).
        deribit_path: the matching Deribit rows file, or None.
        state: the State to fill. A new one is made if omitted.
        speed: playback speed; 1 is real time.
        loop: start again from the first snapshot after the last.
        sleep: injectable for tests.

    The first snapshot is loaded in the constructor, so the API can answer as soon as it starts.
    """

    def __init__(self, path: str, deribit_path: Optional[str] = None, state: Optional[State] = None,
                 speed: float = 1.0, loop: bool = True, sleep: Callable = asyncio.sleep):
        self.path, self.deribit_path = path, deribit_path
        self.state = state or State()
        self.frames = load_frames(path, self.state.asset)
        if not self.frames:
            raise ValueError(f"{path}: no book snapshots to replay")
        self.deribit = load_deribit(deribit_path, self.state.asset) if deribit_path else []
        self._deribit_ts = [ts for ts, _ in self.deribit]
        self._surfaces: Dict[int, Optional[Surface]] = {}
        if speed <= 0:
            raise ValueError("REPLAY_SPEED must be > 0")
        self.speed, self.loop, self._sleep = float(speed), loop, sleep
        gaps = [(b.ts - a.ts).total_seconds() for a, b in zip(self.frames, self.frames[1:])]
        self.typical_gap_s = statistics.median(gaps) if gaps else 30.0
        self.index = -1
        self.loops = 0
        self._stop = asyncio.Event()
        st = self.state
        st.mode, st.replay, st.logger = "replay", self, None
        st.poll_interval_s = self.typical_gap_s
        st.now = lambda: self.ts                           # the virtual clock
        self.step()

    @property
    def ts(self) -> datetime:
        """The current snapshot's timestamp. This is what `state.now()` returns."""
        return self.frames[max(self.index, 0)].ts

    # -- surface
    def _surface_index(self, ts: datetime) -> Optional[int]:
        """Return the index of the latest Deribit record at or before `ts`, or None."""
        i = bisect.bisect_right(self._deribit_ts, ts) - 1
        return i if i >= 0 else None

    def surface_for(self, ts: datetime) -> Tuple[Optional[Surface], Optional[datetime]]:
        """Rebuild (and cache) the surface a live poll would have held at `ts`."""
        i = self._surface_index(ts)
        if i is None:
            return None, None
        if i not in self._surfaces:
            at, rows = self.deribit[i]
            try:
                self._surfaces[i] = build_surface(rows, self.state.asset, at)
            except Exception:
                self._surfaces[i] = None
        return self._surfaces[i], self.deribit[i][0]

    # -- stepping
    def step(self) -> Frame:
        """Move to the next snapshot (wrapping to the first) and load it into the state."""
        nxt = self.index + 1
        if nxt >= len(self.frames):
            nxt = 0
            self.loops += 1
            self.state.history = QuoteHistory()            # time jumps back, so the old history is void
        self.index = nxt
        frame = self.frames[nxt]
        self._apply(frame)
        return frame

    def _apply(self, frame: Frame) -> None:
        st, ts = self.state, frame.ts
        for v in VENUES:
            books = [b for b in frame.books if b.venue == v]
            st.catalog[v] = books
            if books:
                st.set_books(v, books, ts)
            else:
                st.books[v] = {}
        if frame.spot is not None:
            st.set_spot(frame.spot, ts)
        surface, at = self.surface_for(ts)
        st.surface, st.surface_at = surface, at
        if frame.spot is not None and (surface is not None or not self.deribit):
            st.mark_ok("deribit", ts)
        st.match = build_report(frame.books, ts, st.asset)
        st.matched = st.match.matched
        st.finish_poll(nearest_expiries(st.matched, ts, len(st.matched) or 1), ts, 0.0)

    # -- loop
    async def run(self) -> None:
        """Advance through the snapshots at `speed` until `stop()`; loop at the end if `loop` is set."""
        while not self._stop.is_set():
            last = self.index == len(self.frames) - 1
            if last and not self.loop:
                return
            nxt = 0 if last else self.index + 1
            gap = self.typical_gap_s if last else (self.frames[nxt].ts - self.frames[self.index].ts).total_seconds()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, gap / self.speed))
                return
            except asyncio.TimeoutError:
                pass
            # Fit the next surface off the event loop, so requests are not held up by it.
            await asyncio.to_thread(self.surface_for, self.frames[nxt].ts)
            self.step()
            await self._sleep(0)

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        return None

    def info(self) -> dict:
        """The `replay` field of /health."""
        return {"file": self.path, "deribit_file": self.deribit_path,
                "snapshot_ts": iso(self.ts), "snapshot": self.index + 1, "snapshots": len(self.frames),
                "first_ts": iso(self.frames[0].ts), "last_ts": iso(self.frames[-1].ts),
                "speed": self.speed, "loops": self.loops,
                "surface": self.state.surface is not None, "deribit_records": len(self.deribit)}
