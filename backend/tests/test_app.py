"""Offline tests for backend.app. The endpoints read a prepared State; nothing is fetched."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.ingest.poller import Poller, Settings, State
from backend.tests.replay import Replay
from engine import RouteRequest, build_surface, make_fair_fn, price_route
from engine.tests.conftest import EXP, EXP_KEY, mk
from engine.tests.test_fairvalue import deribit_rows, flat

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
SPOT = 65000.0
LADDER_KEYS = {"strike", "gemini", "kalshi", "fair", "stale", "stale_reasons"}
QUOTE_KEYS = {"bid", "ask", "bid_size", "ask_size", "updated"}
ROUTE_KEYS = {"legs", "total_cost", "max_payout", "best_single_venue_cost", "savings", "fair_value",
              "verdict_code", "verdict_text", "fair_note", "stale_warnings", "mixed_settlement_index", "timestamp"}
LEG_KEYS = {"venue", "contract", "side", "price", "fee", "size_available", "link"}


def gemini_books():
    return [mk("gemini", 64000, bids=[(0.60, 250)], asks=[(0.63, 250)]),
            mk("gemini", 66000, bids=[(0.38, 250)], asks=[(0.41, 250)]),
            mk("gemini", 68000, bids=[(0.21, 250)], asks=[(0.24, 250)])]


def kalshi_books():
    return [mk("kalshi", 66000, bids=[(0.39, 900)], asks=[(0.40, 400)]),
            mk("kalshi", 68000, bids=[(0.26, 900)], asks=[(0.27, 400)]),
            mk("kalshi", 70000, bids=[(0.10, 900)], asks=[(0.12, 400)])]


class Env:
    """A State filled the way one good poll would fill it, and a client reading it."""

    def __init__(self, now=NOW, gemini=True, kalshi=True, deribit=True, polled=True):
        self.t = now
        self.state = st = State(now=lambda: self.t)
        if gemini:
            st.set_books("gemini", gemini_books())
        if kalshi:
            st.set_books("kalshi", kalshi_books())
        if deribit:
            st.set_spot(SPOT)
            st.surface = build_surface(deribit_rows(flat(0.5)), "BTC", NOW)
            st.mark_ok("deribit")
        if polled:
            st.finish_poll([EXP_KEY], duration_s=1.5)
        self.client = TestClient(create_app(state=st))


@pytest.fixture
def env():
    return Env()


def ladder(env, **params):
    return env.client.get("/ladder", params={"asset": "BTC", "expiry": EXP_KEY, **params})


def route(env, **body):
    return env.client.post("/route", json={"asset": "BTC", "expiry": EXP_KEY, **body})


# ---- /health -----------------------------------------------------------------------------
def test_health_starting_serves_503_for_data():
    env = Env(gemini=False, kalshi=False, deribit=False, polled=False)
    r = env.client.get("/health")
    assert r.status_code == 200
    h = r.json()
    assert h["status"] == "starting" and h["mode"] == "live" and h["poll_interval_s"] == 30
    assert h["last_poll"] is None and h["polls"] == 0 and h["snapshot_log"] is None
    assert h["venues"]["gemini"] == {"ok": None, "last_success": None, "books": 0, "requests": 0,
                                     "http_429": 0, "last_error": None, "last_error_at": None}
    for r in (env.client.get("/expiries"), ladder(env), route(env, type="above", lo=68000)):
        assert r.status_code == 503 and "Starting" in r.json()["detail"]


def test_health_ok(env):
    h = env.client.get("/health").json()
    assert set(h) == {"status", "mode", "poll_interval_s", "last_poll", "polls", "last_poll_duration_s",
                      "loop_errors", "venues", "expiries", "match", "snapshot_log", "replay"}
    assert h["status"] == "ok" and h["last_poll"] == "2026-11-16T12:00:00Z"
    assert h["polls"] == 1 and h["expiries"] == 1 and h["last_poll_duration_s"] == 1.5
    assert set(h["venues"]) == {"gemini", "kalshi", "deribit"}
    assert h["venues"]["gemini"]["books"] == 3 and h["venues"]["kalshi"]["books"] == 3
    assert "books" not in h["venues"]["deribit"]
    assert all(v["ok"] and v["last_success"] == "2026-11-16T12:00:00Z" for v in h["venues"].values())


def test_health_degraded_after_three_intervals(env):
    env.t = NOW + timedelta(seconds=90)
    assert env.client.get("/health").json()["status"] == "ok"
    env.t = NOW + timedelta(seconds=91)
    r = env.client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "degraded"
    assert ladder(env).status_code == 200                           # last good data is still served


def test_health_degraded_when_a_source_never_answered():
    env = Env(kalshi=False)
    h = env.client.get("/health").json()
    assert h["status"] == "degraded" and h["venues"]["kalshi"]["ok"] is None


# ---- /expiries ---------------------------------------------------------------------------
def test_expiries(env):
    r = env.client.get("/expiries", params={"asset": "BTC"})
    assert r.status_code == 200 and r.json() == [EXP_KEY]
    assert env.client.get("/expiries").json() == [EXP_KEY]          # asset defaults to BTC
    assert env.client.get("/expiries", params={"asset": "btc"}).json() == [EXP_KEY]


def test_unsupported_asset_is_422(env):
    assert env.client.get("/expiries", params={"asset": "DOGE"}).status_code == 422
    assert ladder(env, asset="DOGE").status_code == 422
    assert route(env, asset="DOGE", type="above", lo=68000).status_code == 422


def test_expiry_that_passed_between_polls_is_not_served(env):
    assert env.client.get("/expiries").json() == [EXP_KEY]
    env.t = EXP + timedelta(seconds=1)                               # no poll has run since
    assert env.client.get("/expiries").json() == []
    r = ladder(env)
    assert r.status_code == 404 and "Available: none" in r.json()["detail"]
    assert route(env, type="above", lo=68000).status_code == 404


# ---- /ladder -----------------------------------------------------------------------------
def test_ladder_shape(env):
    r = ladder(env)
    assert r.status_code == 200
    rows = r.json()
    assert [row["strike"] for row in rows] == [64000, 66000, 68000, 70000]
    for row in rows:
        assert set(row) == LADDER_KEYS
        assert set(row["stale"]) == {"gemini", "kalshi"} == set(row["stale_reasons"])
        for v in ("gemini", "kalshi"):
            assert row[v] is None or set(row[v]) == QUOTE_KEYS
    by_k = {row["strike"]: row for row in rows}
    assert by_k[64000]["kalshi"] is None and by_k[70000]["gemini"] is None
    assert by_k[68000]["gemini"] == {"bid": 0.21, "ask": 0.24, "bid_size": 250, "ask_size": 250,
                                     "updated": by_k[68000]["gemini"]["updated"]}
    assert by_k[68000]["kalshi"]["bid"] == 0.26 and by_k[68000]["kalshi"]["ask_size"] == 400
    fair = [row["fair"] for row in rows]
    assert all(0 < f < 1 for f in fair) and fair == sorted(fair, reverse=True)


def test_ladder_fair_null_near_expiry():
    env = Env(now=EXP - timedelta(hours=1))
    rows = ladder(env).json()
    assert rows and all(row["fair"] is None for row in rows)


def test_ladder_unknown_or_bad_expiry(env):
    r = ladder(env, expiry="2026-12-25T22:00")
    assert r.status_code == 404 and EXP_KEY in r.json()["detail"]
    assert ladder(env, expiry="not-a-date").status_code == 422
    assert env.client.get("/ladder").status_code == 422              # expiry is required


def test_ladder_accepts_full_iso_expiry(env):
    assert ladder(env, expiry="2026-11-20T22:00:00Z").json() == ladder(env).json()


# ---- /route ------------------------------------------------------------------------------
def test_route_above_matches_engine(env):
    r = route(env, type="above", lo=68000, size=10)
    assert r.status_code == 200
    got = r.json()
    assert ROUTE_KEYS <= set(got) and all(LEG_KEYS <= set(l) for l in got["legs"])

    surface = build_surface(deribit_rows(flat(0.5)), "BTC", NOW)
    want = price_route(RouteRequest(expiry=EXP_KEY, type="above", lo=68000, size=10),
                       gemini_books() + kalshi_books(), fair_fn=make_fair_fn(surface), now=NOW)
    assert got["legs"][0]["venue"] == "gemini" and got["legs"][0]["side"] == "YES"
    assert got["total_cost"] == pytest.approx(want.total_cost)
    assert got["best_single_venue_cost"] == pytest.approx(want.best_single_venue_cost)
    assert got["fair_value"] == pytest.approx(want.fair_value)
    assert got["verdict_code"] == want.verdict_code and got["verdict_code"] in ("options_higher", "in_line", "options_lower")
    assert got["verdict_text"] == want.verdict_text and "crash protection" in got["fair_note"]
    assert got["max_payout"] == 1.0 and got["build"] == "single"


def test_route_between_is_two_leg(env):
    got = route(env, type="between", lo=66000, hi=68000, size=10).json()
    assert got["build"] == "two_leg" and got["guaranteed_payout"] == 1.0
    assert {(l["side"], l["strike_lo"], l["strike_hi"]) for l in got["legs"]} == \
        {("YES", 66000, None), ("NO", None, 68000)}
    assert got["total_cost"] == pytest.approx(got["cash_outlay"] - 1.0)
    assert 0 < got["fair_value"] < 1


def test_route_size_defaults_to_one(env):
    got = route(env, type="below", hi=68000).json()
    assert got["size"] == 1 and got["legs"][0]["side"] == "NO"


def test_route_verdict_na_near_expiry():
    env = Env(now=EXP - timedelta(hours=1))
    got = route(env, type="above", lo=68000).json()
    assert got["fair_value"] is None and got["verdict_code"] == "unavailable" and got["fair_note"] is None
    assert "2 hours" in got["verdict_text"]


def test_route_strike_not_found_is_422_with_nearest(env):
    r = route(env, type="above", lo=68100)
    assert r.status_code == 422
    assert r.json() == {"detail": "Strike 68100 is not listed for BTC 2026-11-20T22:00. "
                                  "Nearest listed strikes: 68000, 70000",
                        "nearest": [68000, 70000]}


def test_route_bad_body_is_422(env):
    assert route(env, type="between", lo=68000, hi=66000).status_code == 422
    assert route(env, type="above").status_code == 422
    assert route(env, type="sideways", lo=68000).status_code == 422
    assert route(env, type="above", lo=68000, size=0).status_code == 422


def test_route_unknown_expiry_is_404(env):
    assert route(env, expiry="2026-12-25T22:00", type="above", lo=68000).status_code == 404


# ---- missing data ------------------------------------------------------------------------
def test_one_venue_missing_serves_the_other():
    env = Env(gemini=False)
    rows = ladder(env).json()
    assert [row["strike"] for row in rows] == [66000, 68000, 70000]
    assert all(row["gemini"] is None for row in rows)
    assert route(env, type="above", lo=68000).json()["legs"][0]["venue"] == "kalshi"


def test_no_surface_serves_without_fair():
    env = Env(deribit=False)
    rows = ladder(env).json()
    assert len(rows) == 4 and all(row["fair"] is None for row in rows)
    got = route(env, type="above", lo=68000).json()
    assert got["fair_value"] is None and got["verdict_code"] == "unavailable"
    assert got["verdict_text"] == "Options fair value unavailable"


def test_no_books_for_a_polled_expiry_is_503():
    env = Env(gemini=False, kalshi=False)
    r = ladder(env)
    assert r.status_code == 503 and "No books" in r.json()["detail"]
    assert env.client.get("/health").status_code == 200


# ---- poller lifecycle --------------------------------------------------------------------
def test_poller_starts_with_the_app_and_endpoints_never_fetch():
    replay = Replay()
    state = State(now=lambda: replay.now)
    settings = Settings(poll_interval_s=3600.0, gemini_rps=1e6, kalshi_rps=1e6, deribit_rps=1e6)
    poller = Poller(state, settings, transport=replay.transport)
    with TestClient(create_app(poller=poller)) as client:           # entering runs the lifespan
        deadline = time.monotonic() + 20
        while client.get("/health").json()["status"] == "starting":
            assert time.monotonic() < deadline, "the poller never completed a poll"
            time.sleep(0.02)
        fetched = sum(replay.hits.values())
        assert fetched > 0

        targets = replay.meta["targets"]
        assert client.get("/expiries").json() == targets
        for e in targets:
            rows = client.get("/ladder", params={"expiry": e}).json()
            assert rows and any(row["gemini"] and row["kalshi"] for row in rows)
        strike = next(row["strike"] for row in rows if row["gemini"] and row["gemini"]["ask"])
        r = client.post("/route", json={"expiry": targets[-1], "type": "above", "lo": strike, "size": 5})
        assert r.status_code == 200 and r.json()["fair_value"] is not None
        assert client.get("/health").json()["status"] == "ok"
        assert sum(replay.hits.values()) == fetched                 # the endpoints made no requests
    assert poller._stop.is_set()                                    # leaving stops the poller


# ---- CORS --------------------------------------------------------------------------------
def test_cors_open_by_default(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    c = TestClient(create_app(state=State()))
    r = c.get("/health", headers={"Origin": "https://anything.example"})
    assert r.headers["access-control-allow-origin"] == "*"


def test_cors_restricted_by_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example, https://other.example")
    c = TestClient(create_app(state=State()))
    ok = c.get("/health", headers={"Origin": "https://app.example"})
    assert ok.headers["access-control-allow-origin"] == "https://app.example"
    no = c.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in no.headers


# ---- /receipt, /verify -------------------------------------------------------------------
RECEIPT_KEYS = {"route_id", "hash", "signature", "explorer_url", "status", "created_at", "error"}


def _route_and_request(env):
    req = {"asset": "BTC", "expiry": EXP_KEY, "type": "above", "lo": 68000, "hi": None, "size": 10}
    r = env.client.post("/route", json=req)
    assert r.status_code == 200
    return r.json(), req


def test_receipt_pending_without_chain(env):
    route_body, req = _route_and_request(env)
    r = env.client.post("/receipt", json={"request": req, "route": route_body})
    assert r.status_code == 200
    rec = r.json()
    assert set(rec) == RECEIPT_KEYS
    assert rec["status"] == "pending" and rec["signature"] is None and rec["explorer_url"] is None
    assert len(rec["hash"]) == 64 and rec["hash"].startswith(rec["route_id"])
    again = env.client.post("/receipt", json={"request": req, "route": route_body}).json()
    assert again["route_id"] == rec["route_id"]                     # idempotent per route
    v = env.client.get(f"/verify/{rec['route_id']}").json()
    assert v["status"] == "pending" and v["hash"] == rec["hash"] and v["explorer_url"] is None


def test_receipt_accepts_bare_route(env):
    route_body, req = _route_and_request(env)
    bare = env.client.post("/receipt", json=route_body).json()
    wrapped = env.client.post("/receipt", json={"request": req, "route": route_body}).json()
    assert bare["status"] == "pending" and bare["route_id"] != wrapped["route_id"]


def test_receipt_rejects_bad_body_and_unknown_id(env):
    assert env.client.post("/receipt", json={"route": {"legs": "nope"}}).status_code == 422
    assert env.client.post("/receipt", json=[1, 2]).status_code == 422
    assert env.client.get("/verify/doesnotexist").status_code == 404


def test_receipt_with_chain_module():
    from types import SimpleNamespace

    from fastapi import APIRouter

    written = {}
    extra = APIRouter()

    @extra.get("/chain/ping")
    def ping():
        return {"ok": True}

    def anchor_hash(h):
        written["sig"] = h
        return {"signature": "SIG" + h[:8], "explorer_url": f"https://explorer.solana.com/tx/SIG{h[:8]}?cluster=devnet"}

    chain = SimpleNamespace(router=extra, anchor_hash=anchor_hash,
                            verify_hash=lambda sig, h: sig == "SIG" + h[:8])
    e = Env()
    client = TestClient(create_app(state=e.state, chain=chain))
    assert client.get("/chain/ping").json() == {"ok": True}          # chain.router is mounted
    route_body = client.post("/route", json={"asset": "BTC", "expiry": EXP_KEY, "type": "above",
                                             "lo": 68000, "size": 10}).json()
    rec = client.post("/receipt", json={"request": None, "route": route_body}).json()
    assert rec["status"] == "confirmed" and rec["explorer_url"].endswith("cluster=devnet")
    v = client.get(f"/verify/{rec['route_id']}").json()
    assert v["status"] == "verified" and v["explorer_url"] == rec["explorer_url"]


def test_receipt_chain_failure_stays_pending():
    from types import SimpleNamespace

    def boom(h):
        raise RuntimeError("devnet down")

    e = Env()
    client = TestClient(create_app(state=e.state, chain=SimpleNamespace(anchor_hash=boom)))
    route_body = client.post("/route", json={"asset": "BTC", "expiry": EXP_KEY, "type": "above",
                                             "lo": 68000, "size": 10}).json()
    rec = client.post("/receipt", json={"route": route_body}).json()
    assert rec["status"] == "pending" and "devnet down" in rec["error"]


def test_verify_detects_tampering():
    from fastapi import FastAPI

    from backend.receipts import make_router

    store = {}
    app = FastAPI()
    app.include_router(make_router(None, store))
    c = TestClient(app)
    books = gemini_books() + kalshi_books()
    rt = price_route(RouteRequest(expiry=EXP_KEY, type="above", lo=68000), books, now=NOW)
    rec = c.post("/receipt", json=rt.model_dump(mode="json")).json()
    store[rec["route_id"]]["route"]["total_cost"] = 0.01             # someone edits the stored route
    assert c.get(f"/verify/{rec['route_id']}").json()["status"] == "mismatch"


def test_real_chain_without_signer_stays_pending_and_routes_work(env):
    import chain
    client = TestClient(create_app(state=env.state, chain=chain))
    route_body, req = _route_and_request(env)
    rec = client.post("/receipt", json={"request": req, "route": route_body}).json()
    assert rec["status"] == "pending" and rec["signature"] is None
    assert "no signer configured" in rec["error"]
    assert client.post("/route", json=req).status_code == 200


def test_real_chain_with_mocked_rpc(monkeypatch):
    import chain
    from solders.keypair import Keypair
    from chain.tests.fake_rpc import FakeLedger
    monkeypatch.setenv("SOLANA_KEYPAIR", str(Keypair()))
    ledger = FakeLedger()
    chain.set_rpc(ledger)
    e = Env()
    client = TestClient(create_app(state=e.state, chain=chain))
    route_body, req = _route_and_request(e)
    rec = client.post("/receipt", json={"request": req, "route": route_body}).json()
    assert rec["status"] == "confirmed" and ledger.txs[rec["signature"]] == f"better:v1:{rec['hash']}"
    assert client.get(f"/verify/{rec['route_id']}").json()["status"] == "verified"


def test_standalone_fair_value_matches_route(env):
    params = dict(asset="BTC", expiry=EXP_KEY, type="between", lo=66000, hi=68000)
    reference = env.client.get("/fair-value", params=params)
    assert reference.status_code == 200
    assert reference.json()["fair_value"] == pytest.approx(env.client.post("/route", json=params).json()["fair_value"])


def test_grok_uses_priced_snapshot_and_handles_missing_credentials(env, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    result = route(env, type="between", lo=66000, hi=68000, size=10).json()
    body = {"snapshot": result["grok_snapshot"], "topic": "depth"}
    assert env.client.post("/grok/explain", json=body).status_code == 503
    monkeypatch.setenv("XAI_API_KEY", "test-only")

    async def reply(client, url, *, headers, json):
        assert url == "https://api.x.ai/v1/responses"
        assert result["legs"][0]["contract"] in json["input"]
        from httpx import Request, Response
        return Response(200, request=Request("POST", url), json={"status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Depth covers the request."}]}]})

    monkeypatch.setattr("backend.grok.httpx.AsyncClient.post", reply)
    answer = env.client.post("/grok/explain", json=body)
    assert answer.json()["answer"].startswith("Depth covers the request.")
    assert "$1 outside the requested range and $2 inside" in answer.json()["answer"]
    assert "live venue quotes" in answer.json()["answer"]
    assert env.client.post("/grok/explain", json={**body, "snapshot": "0"*64}).status_code == 404
