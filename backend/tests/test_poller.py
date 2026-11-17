"""Offline tests for the poller and shared state, replaying one recorded live poll."""
import asyncio
from datetime import timedelta

import httpx
import pytest

from backend.ingest import kalshi
from backend.ingest.poller import Poller, Settings, State, nearest_expiries
from backend.tests.replay import Replay
from engine import expiry_key

INTERVAL = 30.0


async def _instant(_seconds):
    await asyncio.sleep(0)


class Env:
    """A poller wired to the recording, with a clock the test can move."""

    def __init__(self, **settings):
        self.replay = Replay()
        self.t = self.replay.now
        self.state = State(now=lambda: self.t)
        kw = dict(gemini_rps=1e6, kalshi_rps=1e6, deribit_rps=1e6, poll_interval_s=INTERVAL)
        kw.update(settings)
        self.poller = Poller(self.state, Settings(**kw), transport=self.replay.transport, sleep=_instant)

    def poll(self, advance_s=0.0):
        self.t += timedelta(seconds=advance_s)
        asyncio.run(self.poller.poll_once())
        return self.state

    def down(self, *hosts, status=500, only=""):
        """Make every request to `hosts` whose URL contains `only` fail with `status`."""
        def fail(request):
            if request.url.host in hosts and only in str(request.url):
                return httpx.Response(status, headers={"Retry-After": "0"})
        self.replay.fail = fail if hosts else None


GEMINI, KALSHI, DERIBIT = "api.gemini.com", "api.elections.kalshi.com", "www.deribit.com"


@pytest.fixture
def env():
    return Env()


# ---- a healthy poll ----------------------------------------------------------------------
def test_poll_fills_state(env):
    st = env.poll()
    targets = env.replay.meta["targets"]
    assert st.expiries == targets and len(targets) == 3
    assert st.polls == 1 and st.status() == "ok" and st.last_poll == env.t
    assert st.spot == env.replay.meta["spot"] and st.surface is not None and st.deribit_rows
    assert all(s.ok and s.last_success == env.t and s.last_error is None for s in st.sources.values())
    for e in targets:
        books = st.books_for(e)
        for venue in ("gemini", "kalshi"):
            assert any(b.venue == venue and b.kind == "threshold" for b in books), (e, venue)
    assert {expiry_key(b.expiry) for v in st.books.values() for b in v.values()} <= set(targets)
    # history feeds the stale detector
    assert len(st.spot_history) == 1
    two_sided = [b for b in st.books["gemini"].values() if b.mid is not None]
    assert two_sided and all(len(st.history.series(b.contract_id)) == 1 for b in two_sided)


def test_request_and_429_counts_reach_health(env):
    st = env.poll()
    h = st.health()
    assert h["status"] == "ok" and h["polls"] == 1 and h["expiries"] == 3
    assert h["venues"]["gemini"]["requests"] == env.replay.count(GEMINI) > 1
    assert h["venues"]["kalshi"]["requests"] == env.replay.count(KALSHI) > 2
    assert h["venues"]["deribit"]["requests"] == 2
    assert all(v["http_429"] == 0 and v["last_error"] is None for v in h["venues"].values())
    assert h["venues"]["gemini"]["books"] == len(st.books["gemini"])
    assert "books" not in h["venues"]["deribit"]


def test_full_depth_only_inside_the_window(env):
    st = env.poll()
    spot, targets = st.spot, set(st.expiries)
    win = (spot * 0.97, spot * 1.03)
    listed = asyncio.run(kalshi.list_markets(env.poller.clients["kalshi"], "BTC"))
    listed = [(s, m) for s, m in listed if kalshi.expiry_of(m) in targets]
    inside = {m["ticker"] for s, m in listed if kalshi.in_window(m, win)}
    outside = {m["ticker"] for s, m in listed} - inside
    assert inside and outside
    asked = {k.split("/markets/")[1].split("/orderbook")[0] for k in env.replay.hits if "/orderbook" in k}
    assert asked == inside                                   # one orderbook request per in-window market
    assert len(st.books["kalshi"]) == len(listed)            # everything else came from the listing
    assert all(len(st.books["kalshi"][t].yes_bids) <= 1 and len(st.books["kalshi"][t].yes_asks) <= 1
               for t in outside)
    assert any(len(st.books["kalshi"][t].yes_bids) > 1 or len(st.books["kalshi"][t].yes_asks) > 1
               for t in inside)


def test_polls_only_the_nearest_expiries():
    env = Env(n_expiries=2)
    st = env.poll()
    assert st.expiries == env.replay.meta["targets"][:2]
    assert len(st.matched) >= 3                              # more are matched than polled
    assert {expiry_key(b.expiry) for v in st.books.values() for b in v.values()} == set(st.expiries)


def test_nearest_expiries_skips_the_past(env):
    now = env.replay.now
    keys = [expiry_key(now + timedelta(hours=h)) for h in (-5, -1, 2, 30, 80, 200)]
    assert nearest_expiries(reversed(keys), now, 3) == keys[2:5]
    assert nearest_expiries(keys[:2], now, 3) == []


# ---- independent failures ----------------------------------------------------------------
def test_kalshi_down_keeps_last_good_and_gemini_carries_on(env):
    st = env.poll()
    t1, kalshi_books = env.t, dict(st.books["kalshi"])
    env.down(KALSHI)
    st = env.poll(advance_s=INTERVAL)
    assert st.polls == 2
    assert st.sources["gemini"].ok and st.sources["gemini"].last_success == env.t
    assert st.sources["deribit"].ok and st.sources["deribit"].last_success == env.t
    k = st.sources["kalshi"]
    assert k.ok is False and k.last_success == t1 and "500" in k.last_error and k.last_error_at == env.t
    assert st.books["kalshi"] == kalshi_books                # last good data is still served
    assert st.expiries == env.replay.meta["targets"]
    assert st.status() == "ok"                               # one missed poll is not degraded yet

    env.poll(advance_s=INTERVAL)
    env.poll(advance_s=INTERVAL)
    assert st.status() == "ok"                               # exactly 3 intervals old
    env.poll(advance_s=INTERVAL)
    assert st.status() == "degraded"                         # older than 3 intervals
    assert st.health()["venues"]["kalshi"]["ok"] is False

    env.down()
    env.poll(advance_s=INTERVAL)
    assert st.status() == "ok" and k.ok is True
    assert k.last_error is not None                          # the last error stays visible after recovery


def test_gemini_down_from_the_start(env):
    env.down(GEMINI)
    st = env.poll()
    assert st.polls == 1 and st.status() == "degraded"
    assert st.sources["gemini"].ok is False and st.sources["gemini"].last_success is None
    assert st.sources["kalshi"].ok is True and st.sources["deribit"].ok is True
    assert st.expiries == [] and st.books["gemini"] == {}    # no match is possible without both listings
    assert st.spot is not None and st.surface is not None


def test_deribit_down_means_top_of_book_and_no_fair(env):
    env.down(DERIBIT)
    st = env.poll()
    assert st.sources["deribit"].ok is False and st.spot is None and st.surface is None
    assert st.sources["gemini"].ok and st.sources["kalshi"].ok
    assert env.replay.count("/orderbook") == 0               # no spot, so no full-depth window
    assert st.books["kalshi"] and all(len(b.yes_bids) <= 1 for b in st.books["kalshi"].values())
    assert st.expiries == env.replay.meta["targets"] and st.status() == "degraded"


def test_deribit_keeps_last_surface(env):
    st = env.poll()
    surface, spot = st.surface, st.spot
    env.down(DERIBIT)
    env.poll(advance_s=INTERVAL)
    assert st.surface is surface and st.spot == spot and st.sources["deribit"].ok is False
    assert env.replay.count("/orderbook") > 0


def test_one_bad_gemini_book_keeps_its_last_good_copy(env):
    st = env.poll()
    symbol = sorted(st.books["gemini"])[0]
    before, n = st.books["gemini"][symbol], len(st.books["gemini"])
    env.down(GEMINI, only=f"/v1/book/{symbol}")
    env.poll(advance_s=INTERVAL)
    g = st.sources["gemini"]
    assert g.ok is True and g.last_success == env.t
    assert f"1 of {n} book requests failed" in g.last_error
    assert len(st.books["gemini"]) == n and st.books["gemini"][symbol] is before


def test_all_gemini_books_failing_is_a_venue_failure(env):
    st = env.poll()
    t1, books = env.t, dict(st.books["gemini"])
    env.down(GEMINI, only="/v1/book/")
    env.poll(advance_s=INTERVAL)
    g = st.sources["gemini"]
    assert g.ok is False and g.last_success == t1 and "all" in g.last_error
    assert st.books["gemini"] == books


def test_kalshi_orderbook_failures_fall_back_to_top_of_book(env):
    env.down(KALSHI, only="/orderbook")
    st = env.poll()
    k = st.sources["kalshi"]
    assert k.ok is True and "full-depth requests failed" in k.last_error
    assert st.books["kalshi"] and all(len(b.yes_bids) <= 1 and len(b.yes_asks) <= 1
                                      for b in st.books["kalshi"].values())


def test_429s_are_counted_and_retried(env):
    seen = {"n": 0}

    def fail(request):
        if "/orderbook" in str(request.url) and seen["n"] < 3:      # fewer than the 4 attempts
            seen["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"})
    env.replay.fail = fail
    st = env.poll()
    assert st.stats["kalshi"].http_429 == 3 and st.health()["venues"]["kalshi"]["http_429"] == 3
    assert st.sources["kalshi"].ok is True and st.sources["kalshi"].last_error is None   # retries succeeded
    assert st.stats["gemini"].http_429 == 0


# ---- the loop ----------------------------------------------------------------------------
def test_loop_survives_an_unexpected_error():
    env = Env(poll_interval_s=0.0)
    calls = {"n": 0}
    real = env.poller.poll_once

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        await real()
        env.poller.stop()

    env.poller.poll_once = flaky
    asyncio.run(asyncio.wait_for(env.poller.run(), timeout=20))
    assert calls["n"] == 2 and env.state.loop_errors == 1
    assert env.state.polls == 1 and env.state.status() == "ok"
    assert env.state.health()["loop_errors"] == 1


def test_loop_stops_on_request():
    env = Env(poll_interval_s=3600.0)

    async def go():
        task = asyncio.create_task(env.poller.run())
        while env.state.polls == 0:
            await asyncio.sleep(0.01)
        env.poller.stop()
        await asyncio.wait_for(task, timeout=5)
        await env.poller.aclose()

    asyncio.run(go())
    assert env.state.polls == 1


# ---- settings ----------------------------------------------------------------------------
def test_settings_from_env(monkeypatch):
    for k in ("POLL_INTERVAL_S", "N_EXPIRIES", "KALSHI_WINDOW_PCT", "GEMINI_RPS", "KALSHI_RPS",
              "DERIBIT_RPS", "VENUE_CONCURRENCY"):
        monkeypatch.delenv(k, raising=False)
    d = Settings.from_env()
    assert (d.poll_interval_s, d.n_expiries, d.window_pct, d.kalshi_rps) == (30.0, 3, 0.03, 12.0)
    monkeypatch.setenv("POLL_INTERVAL_S", "10")
    monkeypatch.setenv("KALSHI_RPS", "4.5")
    monkeypatch.setenv("N_EXPIRIES", "2")
    s = Settings.from_env()
    assert (s.poll_interval_s, s.kalshi_rps, s.n_expiries) == (10.0, 4.5, 2)
    assert Poller(settings=s).clients["kalshi"].limiter.rate == 4.5
