"""Shared state and the background poller that keeps it fresh.

Every `poll_interval_s` the poller refreshes the nearest matched expiries. Each source
(gemini, kalshi, deribit) fails on its own: its last good data stays in `State`, the error is
recorded, and the loop carries on.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import httpx

from engine import Book, Surface, build_surface, expiry_key
from engine.books import iso, to_utc
from engine.stale import History

from . import deribit, gemini, kalshi
from .http import ClientStats, VenueClient

log = logging.getLogger("backend.ingest")
SOURCES = ("gemini", "kalshi", "deribit")
VENUES = ("gemini", "kalshi")
DEGRADED_AFTER_POLLS = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass
class Settings:
    """Poller configuration. `from_env()` reads the upper-case names from the environment."""
    asset: str = "BTC"
    poll_interval_s: float = 30.0     # POLL_INTERVAL_S
    n_expiries: int = 3               # N_EXPIRIES: how many of the nearest matched expiries to poll
    window_pct: float = 0.03          # KALSHI_WINDOW_PCT: full Kalshi depth within this of spot
    gemini_rps: float = 5.0           # GEMINI_RPS
    kalshi_rps: float = 12.0          # KALSHI_RPS
    deribit_rps: float = 5.0          # DERIBIT_RPS
    concurrency: int = 4              # VENUE_CONCURRENCY: requests in flight per venue

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        return cls(asset=d.asset,
                   poll_interval_s=_env("POLL_INTERVAL_S", d.poll_interval_s),
                   n_expiries=int(_env("N_EXPIRIES", d.n_expiries)),
                   window_pct=_env("KALSHI_WINDOW_PCT", d.window_pct),
                   gemini_rps=_env("GEMINI_RPS", d.gemini_rps),
                   kalshi_rps=_env("KALSHI_RPS", d.kalshi_rps),
                   deribit_rps=_env("DERIBIT_RPS", d.deribit_rps),
                   concurrency=int(_env("VENUE_CONCURRENCY", d.concurrency)))


@dataclass
class SourceStatus:
    ok: Optional[bool] = None                  # the last cycle's result; None before the first attempt
    last_success: Optional[datetime] = None
    last_error: Optional[str] = None           # sticky: the most recent error, even after recovery
    last_error_at: Optional[datetime] = None


class State:
    """Everything the endpoints read. Only the poller writes to it.

    Attributes:
        expiries: the expiry keys being polled (nearest matched, ascending). GET /expiries returns these.
        matched: every expiry listed on both venues, including ones not polled.
        books: {venue: {contract_id: Book}} for the polled expiries.
        spot: the Deribit index price in $, or None.
        surface: the fitted Deribit surface, or None.
        history: quote mids by contract id and spot under "spot:<ASSET>", for the stale detector.
        polls: completed poll cycles. 0 means "starting".
    """

    def __init__(self, asset: str = "BTC", poll_interval_s: float = 30.0,
                 now: Callable[[], datetime] = _utcnow):
        self.asset = asset.upper()
        self.poll_interval_s = poll_interval_s
        self.now = now
        self.expiries: List[str] = []
        self.matched: List[str] = []
        self.books: Dict[str, Dict[str, Book]] = {v: {} for v in VENUES}
        self.spot: Optional[float] = None
        self.surface: Optional[Surface] = None
        self.deribit_rows: Optional[List[dict]] = None
        self.history = History()
        self.sources: Dict[str, SourceStatus] = {s: SourceStatus() for s in SOURCES}
        self.stats: Dict[str, ClientStats] = {s: ClientStats() for s in SOURCES}
        self.polls = 0
        self.last_poll: Optional[datetime] = None
        self.last_poll_duration_s: Optional[float] = None
        self.loop_errors = 0

    # -- reads
    def books_for(self, expiry: str) -> List[Book]:
        """Return both venues' books for one expiry key."""
        return [b for v in VENUES for b in self.books[v].values() if expiry_key(b.expiry) == expiry]

    @property
    def spot_history(self) -> list:
        return self.history.series(f"spot:{self.asset}")

    # -- writes
    def mark_ok(self, source: str, ts: Optional[datetime] = None) -> None:
        st = self.sources[source]
        st.ok, st.last_success = True, ts or self.now()

    def note_error(self, source: str, err, ts: Optional[datetime] = None) -> None:
        """Record an error without changing `ok` (e.g. a few failed requests in a good cycle)."""
        st = self.sources[source]
        msg = err if isinstance(err, str) else f"{type(err).__name__}: {err}"
        st.last_error, st.last_error_at = msg[:300], ts or self.now()

    def mark_error(self, source: str, err, ts: Optional[datetime] = None) -> None:
        self.note_error(source, err, ts)
        self.sources[source].ok = False

    def set_books(self, venue: str, books: Sequence[Book], ts: Optional[datetime] = None) -> None:
        """Replace one venue's books, record their mids in the history, and mark the venue ok."""
        ts = ts or self.now()
        self.books[venue] = {b.contract_id: b for b in books}
        for b in books:
            self.history.record_book(b, ts)
        self.mark_ok(venue, ts)

    def set_spot(self, price: float, ts: Optional[datetime] = None) -> None:
        self.spot = float(price)
        self.history.record_spot(self.asset, price, ts or self.now())

    def finish_poll(self, expiries: Sequence[str], ts: Optional[datetime] = None,
                    duration_s: Optional[float] = None) -> None:
        self.expiries = list(expiries)
        self.polls += 1
        self.last_poll = ts or self.now()
        self.last_poll_duration_s = duration_s

    # -- health
    def status(self) -> str:
        """'starting' before the first poll ends; 'degraded' if any source is older than 3 poll intervals."""
        if self.polls == 0:
            return "starting"
        now, limit = self.now(), DEGRADED_AFTER_POLLS * self.poll_interval_s
        for st in self.sources.values():
            if st.last_success is None or (now - st.last_success).total_seconds() > limit:
                return "degraded"
        return "ok"

    def health(self) -> dict:
        t = lambda d: iso(d) if d else None
        venues = {}
        for s in SOURCES:
            st, c = self.sources[s], self.stats[s]
            row = {"ok": st.ok, "last_success": t(st.last_success)}
            if s in self.books:
                row["books"] = len(self.books[s])
            row.update(requests=c.requests, http_429=c.http_429, last_error=st.last_error,
                       last_error_at=t(st.last_error_at))
            venues[s] = row
        return {"status": self.status(), "mode": "live", "poll_interval_s": self.poll_interval_s,
                "last_poll": t(self.last_poll), "polls": self.polls,
                "last_poll_duration_s": self.last_poll_duration_s, "loop_errors": self.loop_errors,
                "venues": venues, "expiries": len(self.expiries), "snapshot_log": None}


def nearest_expiries(matched: Sequence[str], now: datetime, n: int) -> List[str]:
    """Return the `n` soonest expiry keys that are still in the future."""
    return [e for e in sorted(matched) if to_utc(e) > now][:n]


class Poller:
    """Refreshes `state` from the three sources.

    Args:
        state: the shared State. A new one is made if omitted.
        settings: rates, interval, and window.
        transport: an httpx transport for all three clients, e.g. `httpx.MockTransport` in tests.
        sleep, clock: injectable for tests.
    """

    def __init__(self, state: Optional[State] = None, settings: Optional[Settings] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 sleep: Callable = asyncio.sleep, clock: Callable[[], float] = time.monotonic):
        self.settings = s = settings or Settings()
        self.state = state or State(s.asset, s.poll_interval_s)
        self.state.poll_interval_s = s.poll_interval_s
        kw = dict(concurrency=s.concurrency, transport=transport, sleep=sleep)
        self.clients: Dict[str, VenueClient] = {
            "gemini": gemini.make_client(s.gemini_rps, **kw),
            "kalshi": kalshi.make_client(s.kalshi_rps, **kw),
            "deribit": deribit.make_client(s.deribit_rps, **kw)}
        for name, c in self.clients.items():
            self.state.stats[name] = c.stats
        self._clock = clock
        self._stop = asyncio.Event()
        self._gem_exp: Optional[Set[str]] = None
        self._kal_exp: Optional[Set[str]] = None

    # -- one cycle
    async def poll_once(self) -> None:
        """Run one refresh cycle. Source failures are recorded in `state`, not raised."""
        s, st = self.settings, self.state
        t0, asset = self._clock(), s.asset
        gem_list, kal_list, _ = await asyncio.gather(
            self._guard("gemini", gemini.list_contracts(self.clients["gemini"], asset)),
            self._guard("kalshi", kalshi.list_markets(self.clients["kalshi"], asset)),
            self._deribit())

        if gem_list is not None:
            self._gem_exp = {gemini.expiry_of(c, e) for c, e in gem_list}
        if kal_list is not None:
            self._kal_exp = {kalshi.expiry_of(m) for series, m in kal_list
                             if series == kalshi.threshold_series(asset)}
        targets = st.expiries
        if self._gem_exp is not None and self._kal_exp is not None:
            st.matched = sorted(self._gem_exp & self._kal_exp)
            targets = nearest_expiries(st.matched, st.now(), s.n_expiries)

        window = (st.spot * (1 - s.window_pct), st.spot * (1 + s.window_pct)) if st.spot else None
        await asyncio.gather(self._gemini_books(gem_list, targets),
                             self._kalshi_books(kal_list, targets, window))
        st.finish_poll(targets, st.now(), round(self._clock() - t0, 3))

    async def _guard(self, source: str, coro):
        try:
            return await coro
        except Exception as e:
            self.state.mark_error(source, e)
            return None

    async def _deribit(self) -> None:
        st, c, asset = self.state, self.clients["deribit"], self.settings.asset
        spot, rows = await asyncio.gather(deribit.fetch_spot(c, asset), deribit.fetch_rows(c, asset),
                                          return_exceptions=True)
        errors = [x for x in (spot, rows) if isinstance(x, Exception)]
        if not isinstance(spot, Exception):
            st.set_spot(spot)
        if not isinstance(rows, Exception):
            try:    # the SVI fit is CPU work, so it runs off the event loop
                st.surface = await asyncio.to_thread(build_surface, rows, asset, st.now())
                st.deribit_rows = rows
            except Exception as e:
                errors.append(e)
        if errors:
            st.mark_error("deribit", errors[0])
        else:
            st.mark_ok("deribit")

    async def _gemini_books(self, listing, targets: Sequence[str]) -> None:
        if listing is None:
            return                                           # listing failed: keep the last good books
        st, asset = self.state, self.settings.asset
        jobs = [(c, e) for c, e in listing if gemini.expiry_of(c, e) in targets]
        results = await gemini.fetch_books(self.clients["gemini"], jobs, asset, st.now())
        books, failed = [], []
        for (c, _), r in zip(jobs, results):
            if isinstance(r, Exception):
                failed.append(r)
                prev = st.books["gemini"].get(c["instrumentSymbol"])
                if prev is not None:
                    books.append(prev)
            else:
                books.append(r)
        if jobs and len(failed) == len(jobs):
            st.mark_error("gemini", f"all {len(jobs)} book requests failed; last: "
                                    f"{type(failed[-1]).__name__}: {failed[-1]}")
            return
        st.set_books("gemini", books)
        if failed:
            st.note_error("gemini", f"{len(failed)} of {len(jobs)} book requests failed (kept last good); "
                                    f"last: {type(failed[-1]).__name__}: {failed[-1]}")

    async def _kalshi_books(self, listing, targets: Sequence[str],
                            window: Optional[Tuple[float, float]]) -> None:
        if listing is None:
            return
        st, c, asset = self.state, self.clients["kalshi"], self.settings.asset
        failed: List[Exception] = []

        async def one(series: str, m: dict) -> Optional[Book]:
            if kalshi.in_window(m, window):
                try:
                    return await kalshi.fetch_book(c, series, m, asset, st.now())
                except Exception as e:                       # fall back to the listing's top of book
                    failed.append(e)
            return kalshi.top_book(series, m, asset, st.now())

        jobs = [(series, m) for series, m in listing if kalshi.expiry_of(m) in targets]
        books = await asyncio.gather(*(one(series, m) for series, m in jobs))
        st.set_books("kalshi", [b for b in books if b is not None])
        if failed:
            st.note_error("kalshi", f"{len(failed)} full-depth requests failed (used top of book); "
                                    f"last: {type(failed[-1]).__name__}: {failed[-1]}")

    # -- loop
    async def run(self) -> None:
        """Poll until `stop()` is called. An unexpected error is logged and counted, never raised."""
        while not self._stop.is_set():
            t0 = self._clock()
            try:
                await self.poll_once()
            except Exception:
                self.state.loop_errors += 1
                log.exception("poll cycle failed")
            wait = max(0.0, self.settings.poll_interval_s - (self._clock() - t0))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        for c in self.clients.values():
            await c.aclose()
