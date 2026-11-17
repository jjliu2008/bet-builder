"""FastAPI server for bet-builder. Shapes follow docs/api_contract.md.

Run with:
    uvicorn backend.app:app

Step 1 of the backend: data comes from `engine.dev_fetch` on demand and is cached for 30 s.
The live poller, matcher, and snapshot logger replace `LiveFetcher` / `Store` later; the
endpoints only talk to `Store`, so they don't change.

Environment:
    ALLOWED_ORIGINS  comma-separated CORS origins. Default "*" (open).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from engine import (Book, Route, RouteError, RouteRequest, StrikeNotFound, Surface, build_ladder,
                    build_surface, expiry_key, fair_above, make_fair_fn, price_route)
from engine import dev_fetch
from engine.books import iso
from engine.stale import History, make_stale_fn, stale_flags

SUPPORTED_ASSETS = ("BTC",)
POLL_INTERVAL_S = 30.0
# Full Kalshi depth costs one request per market. A live run with a 12% window and 6 workers
# drew 105 HTTP 429s out of 578 requests, so the window and concurrency are kept small.
# Markets outside the window still get top-of-book from the event listing, at no extra cost.
KALSHI_WINDOW_PCT = 0.03          # full Kalshi depth only for strikes within this of spot
KALSHI_WORKERS = 2                # parallel Kalshi orderbook requests
VENUES = ("gemini", "kalshi", "deribit")
_HOSTS = {"api.gemini.com": "gemini", "api.elections.kalshi.com": "kalshi", "www.deribit.com": "deribit"}


class Unavailable(RuntimeError):
    """Raised when no data can be served: the venues are down and nothing is cached."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- fetching ----------------------------------------------------------------------------
class CountingSession(requests.Session):
    """A requests session that counts requests and HTTP 429s per venue, for /health."""

    def __init__(self):
        super().__init__()
        self._count_lock = threading.Lock()
        self.counts: Dict[str, Dict[str, int]] = {v: {"requests": 0, "http_429": 0} for v in VENUES}

    def get(self, url, **kw):  # type: ignore[override]
        venue = _HOSTS.get(urlparse(str(url)).netloc)
        if venue:
            with self._count_lock:
                self.counts[venue]["requests"] += 1
        r = super().get(url, **kw)
        if venue and r.status_code == 429:
            with self._count_lock:
                self.counts[venue]["http_429"] += 1
        return r


class LiveFetcher:
    """Public-API fetches through `engine.dev_fetch`. No keys are used."""

    def __init__(self):
        self.session = CountingSession()

    def expiries(self, asset: str) -> List[str]:
        return dev_fetch.list_expiries(asset, self.session)

    def spot(self, asset: str) -> float:
        return dev_fetch.fetch_spot(asset, self.session)

    def gemini(self, asset: str, expiry: str) -> List[Book]:
        return dev_fetch.fetch_gemini_books(asset, expiry, session=self.session)

    def kalshi(self, asset: str, expiry: str, window: Optional[Tuple[float, float]]) -> List[Book]:
        """Full depth inside `window` ($lo, $hi). With no window (spot unknown), top-of-book only."""
        return dev_fetch.fetch_kalshi_books(asset, expiry, depth="full" if window else "top",
                                            strike_range=window, workers=KALSHI_WORKERS,
                                            session=self.session)

    def deribit_rows(self, asset: str) -> List[dict]:
        d = dev_fetch._get(f"{dev_fetch.DERIBIT}/get_book_summary_by_currency",
                           {"currency": asset.upper(), "kind": "option"}, self.session)
        return d["result"]

    def counts(self) -> Dict[str, Dict[str, int]]:
        return {v: dict(c) for v, c in self.session.counts.items()}


# ---- cached store ------------------------------------------------------------------------
@dataclass
class Snapshot:
    """Everything needed to answer /ladder and /route for one asset and expiry."""
    asset: str
    expiry: str
    ts: datetime
    gemini: List[Book]
    kalshi: List[Book]
    spot: Optional[float]
    surface: Optional[Surface]

    @property
    def books(self) -> List[Book]:
        return self.gemini + self.kalshi


@dataclass
class _VenueStatus:
    ok: Optional[bool] = None            # None until the first attempt
    last_success: Optional[datetime] = None
    error: Optional[str] = None


@dataclass
class _Entry:
    at: float
    value: object


class Store:
    """Fetches on demand, caches each result for `ttl` seconds, and tracks venue health.

    Args:
        fetcher: an object with the `LiveFetcher` methods.
        ttl: the cache lifetime in seconds.
        now: returns the evaluation time (aware UTC). Injectable for tests and replay.
        clock: a monotonic clock for the cache. Injectable for tests.
    """

    def __init__(self, fetcher=None, ttl: float = POLL_INTERVAL_S,
                 now: Callable[[], datetime] = _utcnow, clock: Callable[[], float] = time.monotonic):
        self.fetcher = fetcher or LiveFetcher()
        self.ttl = ttl
        self.now = now
        self.clock = clock
        self.history = History()
        self.last_poll: Optional[datetime] = None
        self.status: Dict[str, _VenueStatus] = {v: _VenueStatus() for v in VENUES}
        self._cache: Dict[tuple, _Entry] = {}
        self._locks: Dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()

    # -- helpers
    def _call(self, venue: str, fn: Callable, *args):
        """Run one fetch and record the venue's success or failure. Re-raises on failure."""
        st = self.status[venue]
        try:
            out = fn(*args)
        except Exception as e:
            st.ok, st.error = False, f"{type(e).__name__}: {e}"[:300]
            raise
        st.ok, st.error, st.last_success = True, None, self.now()
        return out

    def _cached(self, key: tuple, build: Callable[[Optional[object]], object]):
        """Return the cached value for `key`, rebuilding it once per `ttl`.

        `build(previous)` gets the last value (or None). If it raises and a previous value
        exists, the previous value is served and retried on the next call.
        """
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            hit = self._cache.get(key)
            if hit and self.clock() - hit.at < self.ttl:
                return hit.value
            try:
                value = build(hit.value if hit else None)
            except Unavailable:
                raise
            except Exception as e:
                if hit:
                    return hit.value
                raise Unavailable(f"{type(e).__name__}: {e}") from e
            self._cache[key] = _Entry(self.clock(), value)
            return value

    # -- data
    def expiries(self, asset: str) -> List[str]:
        """Return the expiry keys listed on both venues, ascending."""
        # The listing needs both venues, so a failure isn't pinned on either one's status.
        return self._cached(("expiries", asset),  # type: ignore[return-value]
                            lambda _prev: sorted(self.fetcher.expiries(asset)))

    def surface(self, asset: str) -> Optional[Surface]:
        """Return the fitted Deribit surface, or None if Deribit has never answered."""
        def build(_prev):
            rows = self._call("deribit", self.fetcher.deribit_rows, asset)
            return build_surface(rows, asset, self.now())
        try:
            return self._cached(("surface", asset), build)  # type: ignore[return-value]
        except Unavailable:
            return None

    def snapshot(self, asset: str, expiry: str) -> Snapshot:
        """Return the books, spot, and surface for one expiry. A venue that fails keeps its last books."""
        def build(prev: Optional[Snapshot]) -> Snapshot:
            try:
                spot = self._call("deribit", self.fetcher.spot, asset)
            except Exception:
                spot = prev.spot if prev else None
            window = (spot * (1 - KALSHI_WINDOW_PCT), spot * (1 + KALSHI_WINDOW_PCT)) if spot else None
            try:
                gem = self._call("gemini", self.fetcher.gemini, asset, expiry)
            except Exception:
                gem = prev.gemini if prev else []
            try:
                kal = self._call("kalshi", self.fetcher.kalshi, asset, expiry, window)
            except Exception:
                kal = prev.kalshi if prev else []
            if not gem and not kal:
                raise Unavailable(f"No books available for {asset} {expiry}: "
                                  f"gemini: {self.status['gemini'].error}; kalshi: {self.status['kalshi'].error}")
            ts = self.now()
            snap = Snapshot(asset=asset, expiry=expiry, ts=ts, gemini=gem, kalshi=kal, spot=spot,
                            surface=self.surface(asset))
            for b in snap.books:
                self.history.record_book(b, ts)
            if spot:
                self.history.record_spot(asset, spot, ts)
            self.last_poll = ts
            return snap
        return self._cached(("snap", asset, expiry), build)  # type: ignore[return-value]

    def stale_inputs(self, snap: Snapshot) -> tuple:
        """Return (fair {strike: p_above} or None, quote history, spot history) for the stale detector."""
        strikes = sorted({b.strike for b in snap.books if b.kind == "threshold"})
        fair = None
        if snap.surface is not None and strikes:
            try:
                fair = fair_above(snap.asset, snap.expiry, strikes, surface=snap.surface)
            except (ValueError, KeyError):
                fair = None
        hist = self.history.as_mapping()
        return fair, hist, hist.get(f"spot:{snap.asset}", [])

    # -- health
    def health(self) -> dict:
        counts = self.fetcher.counts() if hasattr(self.fetcher, "counts") else {}
        snaps = [e.value for k, e in self._cache.items() if k[0] == "snap"]
        books = {"gemini": sum(len(s.gemini) for s in snaps), "kalshi": sum(len(s.kalshi) for s in snaps)}
        venues = {}
        for v in VENUES:
            st, c = self.status[v], counts.get(v, {})
            row = {"ok": st.ok, "last_success": iso(st.last_success) if st.last_success else None}
            if v in books:
                row["books"] = books[v]
            row.update(requests=c.get("requests", 0), http_429=c.get("http_429", 0), error=st.error)
            venues[v] = row
        if self.last_poll is None:
            status = "starting"
        elif all(self.status[v].ok for v in VENUES):
            status = "ok"
        else:
            status = "degraded"
        exp = self._cache.get(("expiries", SUPPORTED_ASSETS[0]))
        return {"status": status, "mode": "live", "poll_interval_s": self.ttl,
                "last_poll": iso(self.last_poll) if self.last_poll else None,
                "venues": venues, "expiries": len(exp.value) if exp else None,  # type: ignore[arg-type]
                "snapshot_log": None}


# ---- app ---------------------------------------------------------------------------------
def _origins() -> List[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "*")
    return [o.strip() for o in raw.split(",") if o.strip()] or ["*"]


def _asset(asset: str) -> str:
    a = asset.upper()
    if a not in SUPPORTED_ASSETS:
        raise HTTPException(422, f"Unsupported asset '{asset}'. Supported: {', '.join(SUPPORTED_ASSETS)}")
    return a


def _snapshot(store: Store, asset: str, expiry: str) -> Snapshot:
    """Validate the expiry against the matched list, then return its snapshot."""
    try:
        key = expiry_key(expiry)
    except ValueError:
        raise HTTPException(422, f"Bad expiry '{expiry}'. Use ISO UTC to the minute, e.g. 2026-11-20T22:00")
    listed = store.expiries(asset)
    if key not in listed:
        raise HTTPException(404, f"Expiry {key} is not listed on both venues for {asset}. "
                                 f"Listed: {', '.join(listed) or 'none'}")
    return store.snapshot(asset, key)


def create_app(store: Optional[Store] = None) -> FastAPI:
    """Build the FastAPI app. Pass a `Store` to inject data (tests, replay)."""
    app = FastAPI(title="bet-builder", version="0.1.0")
    app.state.store = store or Store()
    app.add_middleware(CORSMiddleware, allow_origins=_origins(), allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(StrikeNotFound)
    def _strike_not_found(_: Request, e: StrikeNotFound):
        return JSONResponse(status_code=422, content={"detail": str(e), "nearest": e.nearest})

    @app.exception_handler(RouteError)
    def _route_error(_: Request, e: RouteError):
        return JSONResponse(status_code=422, content={"detail": str(e)})

    @app.exception_handler(Unavailable)
    def _unavailable(_: Request, e: Unavailable):
        return JSONResponse(status_code=503, content={"detail": str(e)})

    @app.get("/health")
    def health(request: Request) -> dict:
        """Always HTTP 200 while the process is up; read `status` for data health."""
        return request.app.state.store.health()

    @app.get("/expiries")
    def expiries(request: Request, asset: str = Query("BTC")) -> List[str]:
        return request.app.state.store.expiries(_asset(asset))

    @app.get("/ladder")
    def ladder(request: Request, expiry: str, asset: str = Query("BTC")) -> List[dict]:
        store: Store = request.app.state.store
        snap = _snapshot(store, _asset(asset), expiry)
        fair, hist, spot_hist = store.stale_inputs(snap)
        now = store.now()
        flags = stale_flags(snap.books, snap.expiry, fair, hist, spot_hist, now)
        return build_ladder(snap.books, snap.asset, snap.expiry, fair, flags, now)

    @app.post("/route", response_model=Route)
    def route(request: Request, body: RouteRequest) -> Route:
        store: Store = request.app.state.store
        body = body.model_copy(update={"asset": _asset(body.asset)})
        snap = _snapshot(store, body.asset, body.expiry)
        fair, hist, spot_hist = store.stale_inputs(snap)
        now = store.now()
        return price_route(
            body, snap.books,
            fair_fn=make_fair_fn(snap.surface) if snap.surface is not None else None,
            stale_fn=make_stale_fn(snap.books, snap.expiry, fair, hist, spot_hist, now),
            now=now)

    return app


app = create_app()
