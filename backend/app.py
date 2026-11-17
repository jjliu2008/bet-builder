"""FastAPI server for bet-builder. Shapes follow docs/api_contract.md.

Run with:
    uvicorn backend.app:app

A background poller (`backend.ingest.poller.Poller`) starts with the app and refreshes the
shared `State` every POLL_INTERVAL_S. The endpoints only read that state; they never fetch.
With REPLAY_FILE set, a `backend.replay.Replayer` fills the state from a recorded log instead,
and no venue is contacted.

Environment:
    ALLOWED_ORIGINS    comma-separated CORS origins. Default "*" (open).
    POLL_INTERVAL_S    seconds between polls. Default 30.
    N_EXPIRIES         nearest matched expiries to poll. Default 3.
    MIN_EXPIRY_HOURS   skip expiries closer than this many hours. Default 0.
    KALSHI_WINDOW_PCT  full Kalshi depth within this fraction of spot. Default 0.03.
    GEMINI_RPS, KALSHI_RPS, DERIBIT_RPS   requests per second per venue. Defaults 5, 12, 5.
    VENUE_CONCURRENCY  requests in flight per venue. Default 4.
    LOG_DIR, LOG_ENABLED, LOG_EVERY_N_POLLS, LOG_MAX_MB   snapshot log; see backend/logger.py.
    REPLAY_FILE, REPLAY_DERIBIT_FILE, REPLAY_SPEED        replay mode; see backend/replay.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from engine import (Book, Route, RouteError, RouteRequest, StrikeNotFound, build_ladder, expiry_key,
                    fair_above, make_fair_fn, price_route)
from engine.stale import make_stale_fn, stale_flags

from .ingest.poller import Poller, Settings, State
from .logger import LogSettings, SnapshotLogger
from .replay import Replayer

SUPPORTED_ASSETS = ("BTC",)


class Unavailable(RuntimeError):
    """Raised when no data can be served yet, or none is held for the request."""


def _origins() -> List[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "*")
    return [o.strip() for o in raw.split(",") if o.strip()] or ["*"]


def _asset(asset: str) -> str:
    a = asset.upper()
    if a not in SUPPORTED_ASSETS:
        raise HTTPException(422, f"Unsupported asset '{asset}'. Supported: {', '.join(SUPPORTED_ASSETS)}")
    return a


def _ready(state: State) -> None:
    if state.polls == 0:
        raise Unavailable("Starting: the first poll has not completed yet")


def _books(state: State, expiry: str) -> tuple:
    """Validate the expiry against the polled list. Return (expiry key, its books)."""
    _ready(state)
    try:
        key = expiry_key(expiry)
    except ValueError:
        raise HTTPException(422, f"Bad expiry '{expiry}'. Use ISO UTC to the minute, e.g. 2026-11-20T22:00")
    live = state.live_expiries
    if key not in live:
        raise HTTPException(404, f"Expiry {key} is not available for {state.asset}. "
                                 f"Available: {', '.join(live) or 'none'}")
    books = state.books_for(key)
    if not books:
        raise Unavailable(f"No books held for {state.asset} {key}")
    return key, books


def _fair(state: State, expiry: str, books: List[Book]) -> Optional[dict]:
    """Return {strike: P(S_T >= K)} from the cached surface, or None without one."""
    strikes = sorted({b.strike for b in books if b.kind == "threshold"})
    if state.surface is None or not strikes:
        return None
    try:
        return fair_above(state.asset, expiry, strikes, surface=state.surface)
    except (ValueError, KeyError):
        return None


def create_app(state: Optional[State] = None, poller: Optional[Poller] = None,
               replayer: Optional[Replayer] = None) -> FastAPI:
    """Build the FastAPI app.

    With no arguments the environment decides: REPLAY_FILE serves a recorded log through a
    `Replayer`; otherwise a live poller is created. Whichever it is starts with the app.
    Pass `state` alone to serve prepared data with nothing running in the background (tests).
    """
    if state is None and poller is None and replayer is None:
        replay_file = os.environ.get("REPLAY_FILE")
        if replay_file:
            replayer = Replayer(replay_file, os.environ.get("REPLAY_DERIBIT_FILE") or None,
                                speed=float(os.environ.get("REPLAY_SPEED") or 1.0))
        else:
            log_settings = LogSettings.from_env()
            poller = Poller(settings=Settings.from_env(),
                            logger=SnapshotLogger(log_settings) if log_settings.enabled else None)
    runner = replayer or poller                            # what keeps the state fresh; never both
    state = state or runner.state  # type: ignore[union-attr]

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(runner.run()) if runner else None
        try:
            yield
        finally:
            if runner and task:
                runner.stop()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await runner.aclose()

    app = FastAPI(title="bet-builder", version="0.1.0", lifespan=lifespan)
    app.state.store = state
    app.state.poller = poller
    app.state.replayer = replayer
    app.add_middleware(CORSMiddleware, allow_origins=_origins(), allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(StrikeNotFound)
    async def _strike_not_found(_: Request, e: StrikeNotFound):
        return JSONResponse(status_code=422, content={"detail": str(e), "nearest": e.nearest})

    @app.exception_handler(RouteError)
    async def _route_error(_: Request, e: RouteError):
        return JSONResponse(status_code=422, content={"detail": str(e)})

    @app.exception_handler(Unavailable)
    async def _unavailable(_: Request, e: Unavailable):
        return JSONResponse(status_code=503, content={"detail": str(e)})

    # The handlers are `async def` so they run on the event loop, where the poller also writes
    # the state. That keeps every read consistent without locks.
    @app.get("/health")
    async def health() -> dict:
        """Always HTTP 200 while the process is up; read `status` for data health."""
        return state.health()

    @app.get("/expiries")
    async def expiries(asset: str = Query("BTC")) -> List[str]:
        _asset(asset)
        _ready(state)
        return state.live_expiries

    @app.get("/ladder")
    async def ladder(expiry: str, asset: str = Query("BTC")) -> List[dict]:
        _asset(asset)
        key, books = _books(state, expiry)
        fair, now = _fair(state, key, books), state.now()
        flags = stale_flags(books, key, fair, state.history.as_mapping(), state.spot_history, now)
        return build_ladder(books, state.asset, key, fair, flags, now)

    @app.post("/route", response_model=Route)
    async def route(body: RouteRequest) -> Route:
        body = body.model_copy(update={"asset": _asset(body.asset)})
        key, books = _books(state, body.expiry)
        fair, now = _fair(state, key, books), state.now()
        return price_route(
            body, books,
            fair_fn=make_fair_fn(state.surface) if state.surface is not None else None,
            stale_fn=make_stale_fn(books, key, fair, state.history.as_mapping(), state.spot_history, now),
            now=now)

    return app


app = create_app()
