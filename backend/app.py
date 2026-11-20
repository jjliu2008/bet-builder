"""FastAPI server for bet-builder. Shapes follow docs/api_contract.md.

Run with:
    uvicorn backend.app:app

A background poller (`backend.ingest.poller.Poller`) starts with the app and refreshes the
shared `State` every POLL_INTERVAL_S. The endpoints only read that state; they never fetch.
With REPLAY_FILE set, a `backend.replay.Replayer` fills the state from a recorded log instead,
and no venue is contacted.

Environment:
    ALLOWED_ORIGINS    comma-separated CORS origins, e.g. "http://localhost:3000,https://bet.example".
                       Default "*" (open). The frontend README uses this same name.
    POLL_INTERVAL_S    seconds between polls. Default 30.
    N_EXPIRIES         nearest matched expiries to poll. Default 8.
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
import logging
import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from engine import (Book, Route, RouteError, RouteRequest, StrikeNotFound, build_ladder, expiry_key,
                    fair_above, make_fair_fn, price_route)
from engine.fairvalue import fair_for_request, fair_hidden
from engine.routes import RouteConfig
from engine.stale import make_stale_fn, stale_flags

from . import asset_markets, xstocks
from .grok import RouteExplainer
from .ingest.poller import Poller, Settings, State
from .logger import LogSettings, SnapshotLogger
from .receipt_store import ReceiptStore
from .receipts import fingerprint, load_chain, make_router, retry_pending
from .replay import Replayer

SUPPORTED_ASSETS = ("BTC", *sorted(asset_markets.EQUITIES), *asset_markets.CRYPTO)


class Unavailable(RuntimeError):
    """Raised when no data can be served yet, or none is held for the request."""


def _origins() -> List[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "*")
    return [o.strip() for o in raw.split(",") if o.strip()] or ["*"]


def _asset(asset: str) -> str:
    a = asset.upper()
    a = {"AAPLX": "AAPL", "NVDAX": "NVDA"}.get(a, a)
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
               replayer: Optional[Replayer] = None, chain=None,
               receipt_store=None) -> FastAPI:
    """Build the FastAPI app.

    With no arguments the environment decides: REPLAY_FILE serves a recorded log through a
    `Replayer`; otherwise a live poller is created. Whichever it is starts with the app.
    Pass `state` alone to serve prepared data with nothing running in the background (tests).
    `chain` is the optional onchain receipts module. By default the `chain` package is imported
    if present; without it, receipts are issued with status "pending".
    `receipt_store` defaults to a `ReceiptStore` (DATABASE_URL Postgres, else SQLite at RECEIPTS_DB).
    """
    route_config = RouteConfig(exclude_crossed_books=True, require_common_settlement=True)
    chain = chain if chain is not None else load_chain()
    store = receipt_store if receipt_store is not None else ReceiptStore()
    if state is None and poller is None and replayer is None:
        replay_file = os.environ.get("REPLAY_FILE")
        if replay_file:
            replayer = Replayer(replay_file, os.environ.get("REPLAY_DERIBIT_FILE") or None,
                                speed=float(os.environ.get("REPLAY_SPEED") or 1.0))
        else:
            log_settings = LogSettings.from_env()
            poller = Poller(settings=Settings.from_env(),
                            logger=SnapshotLogger(log_settings, chain=chain, anchor_store=store)
                            if log_settings.enabled else None)
    runner = replayer or poller                            # what keeps the state fresh; never both
    state = state or runner.state  # type: ignore[union-attr]

    async def _retry_pending_quietly():
        try:
            await retry_pending(store, chain)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad DB or chain at startup must not take the API down
            pass

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(runner.run()) if runner else None
        # Retry receipts left pending (e.g. devnet was down) in the background; never block startup.
        retry = asyncio.create_task(_retry_pending_quietly())
        try:
            yield
        finally:
            retry.cancel()
            if runner and task:
                runner.stop()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await runner.aclose()

    app = FastAPI(title="bet-builder", version="0.1.0", lifespan=lifespan)
    markets = asset_markets.AssetMarkets()
    explainer = RouteExplainer()
    app.post("/grok/explain")(explainer.explain)
    app.include_router(xstocks.router)
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
    @app.get("/release")
    async def release() -> dict:
        return {"revision": os.getenv("RENDER_GIT_COMMIT"),
                "grok_key_configured": bool(os.getenv("XAI_API_KEY")),
                "solana_signer_configured": bool(getattr(chain, "signer_pubkey", lambda: None)()),
                "receipt_storage": getattr(store, "backend", "unknown")}

    @app.get("/health")
    async def health() -> dict:
        """Always HTTP 200 while the process is up; read `status` for data health."""
        return state.health()

    @app.get("/expiries")
    async def expiries(asset: str = Query("BTC")) -> List[str]:
        if _asset(asset) != "BTC":
            return await markets.get(_asset(asset))
        _ready(state)
        return state.live_expiries

    @app.get("/ladder")
    async def ladder(expiry: str, asset: str = Query("BTC")) -> List[dict]:
        asset = _asset(asset)
        if asset != "BTC":
            books = await markets.get(asset, expiry)
            surface = await markets.surface(asset)
            strikes = sorted({b.strike for b in books if b.strike is not None})
            try:
                fair = fair_above(asset, expiry, strikes, surface=surface) if surface and strikes else None
            except (ValueError, KeyError):
                fair = None
            return build_ladder(books, asset, expiry, fair, stale_flags(books, expiry, fair))
        key, books = _books(state, expiry)
        fair, now = _fair(state, key, books), state.now()
        flags = stale_flags(books, key, fair, state.history.as_mapping(), state.spot_history, now)
        return build_ladder(books, state.asset, key, fair, flags, now)

    @app.get("/fair-value")
    async def fair_value(request: RouteRequest = Query()) -> dict:
        asset = _asset(request.asset)
        if asset != "BTC":
            await markets.get(asset, request.expiry)
            surface = await markets.surface(asset)
            value = fair_for_request(request, surface) if surface and not fair_hidden(request.expiry) else None
            return {"fair_value": value, "reason": None if value is not None else "No benchmark for this expiry"}
        _books(state, request.expiry)
        if fair_hidden(request.expiry, state.now()):
            return {"fair_value": None, "reason": "Too close to expiry"}
        value = (fair_for_request(request.model_copy(update={"asset": asset}), state.surface)
                 if state.surface is not None else None)
        return {"fair_value": round(value, 6) if value is not None else None, "reason": None if value is not None else "Options surface unavailable"}

    @app.post("/route", response_model=Route)
    async def route(body: RouteRequest) -> Route:
        body = body.model_copy(update={"asset": _asset(body.asset)})
        mode = state.mode if body.asset == "BTC" else "live"
        if body.asset != "BTC":
            books = await markets.get(body.asset, body.expiry)
            surface = await markets.surface(body.asset)
            observed_history = markets.histories.get((body.asset, expiry_key(body.expiry)))
            history = observed_history.as_mapping() if observed_history else None
            result = price_route(body, books, fair_fn=make_fair_fn(surface) if surface else None,
                                 stale_fn=make_stale_fn(books, body.expiry, quote_history=history),
                                 quote_history=history, config=route_config)
            if body.asset in asset_markets.EQUITIES:
                result.warnings.append("Stock contracts pay YES strictly above the strike. Equality pays NO. Venue rules govern closures and exceptional settlement.")
        else:
            key, books = _books(state, body.expiry)
            fair, now = _fair(state, key, books), state.now()
            history = state.history.as_mapping()
            result = price_route(
                body, books,
                fair_fn=make_fair_fn(state.surface) if state.surface is not None else None,
                stale_fn=make_stale_fn(books, key, fair, history, state.spot_history, now),
                now=now, config=route_config, quote_history=history)

        result.grok_snapshot = explainer.remember(body, result, "replay" if mode == "replay" else "live")
        if hasattr(store, "record_route"):
            selected = {(leg.venue, leg.contract) for leg in result.legs}
            quotes = [book.model_dump(mode="json") for book in books
                      if (book.venue, book.contract_id) in selected]
            refs = {q["settlement_index"].strip() for q in quotes}
            # Only measure savings when every candidate book has the same known
            # settlement and the full requested quantity can be compared.
            benchmark_refs = {b.settlement_index.strip() for b in books}
            comparable = (result.filled_size == body.size and result.savings is not None
                          and len(refs) == 1 and "" not in refs and benchmark_refs == refs)
            observed = state.last_poll.isoformat() if body.asset == "BTC" and state.last_poll else result.timestamp
            query = body.model_dump(mode="json")
            evidence_id = fingerprint(query, {"poll": observed, "mode": mode})
            evidence = {"request": query, "route": result.model_dump(), "selected_books": quotes}
            try:
                await asyncio.to_thread(store.record_route, evidence_id, observed, mode,
                                        result.savings * result.filled_size if comparable else None, evidence)
            except Exception:
                logging.getLogger(__name__).exception("Route evidence write failed")
        return result

    @app.get("/savings")
    async def savings_summary() -> dict:
        if not hasattr(store, "savings_summary"):
            raise Unavailable("Route evidence storage unavailable")
        return await asyncio.to_thread(store.savings_summary)

    if chain is not None and getattr(chain, "router", None) is not None:
        app.include_router(chain.router)
    app.state.chain = chain
    app.state.receipts = store
    app.include_router(make_router(chain, store=store))

    @app.get("/anchors")
    async def anchors() -> List[dict]:
        """The latest 48 hourly snapshot anchors (Merkle roots onchain), newest first."""
        if not hasattr(store, "anchors"):
            return []
        try:
            return store.anchors(48)
        except Exception as e:
            raise Unavailable(f"Anchor store unavailable: {type(e).__name__}: {e}")

    return app


app = create_app()
