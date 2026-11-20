"""Offline tests for the poller and shared state, replaying one recorded live poll."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.ingest import kalshi
from backend.ingest.poller import Poller, Settings, State, nearest_expiries
from backend.tests.replay import Replay
from engine import expiry_key
from engine.books import to_utc

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
    assert nearest_expiries(keys, now, 3, min_hours=2.5) == keys[3:6]        # the one 2 hours out is skipped


def test_min_expiry_hours_skips_the_hourly():
    env = Env(min_expiry_hours=2.0)
    st = env.poll()
    assert st.expiries == env.replay.meta["targets"][1:]                  # 06:00 is 48 minutes away
    assert st.matched == env.replay.meta["targets"]                       # still matched, just not polled


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
    assert (d.poll_interval_s, d.n_expiries, d.window_pct, d.kalshi_rps) == (30.0, 8, 0.03, 12.0)
    monkeypatch.setenv("POLL_INTERVAL_S", "10")
    monkeypatch.setenv("KALSHI_RPS", "4.5")
    monkeypatch.setenv("N_EXPIRIES", "2")
    s = Settings.from_env()
    assert (s.poll_interval_s, s.kalshi_rps, s.n_expiries) == (10.0, 4.5, 2)
    assert Poller(settings=s).clients["kalshi"].limiter.rate == 4.5


# ---- matcher and expiry rollover ---------------------------------------------------------
NEW_EXPIRY = "2026-11-30T22:00"          # in the recording, Gemini lists this and Kalshi does not


def kalshi_starts_listing(replay, src: str, new: str) -> None:
    """Edit the recording so Kalshi lists `new`, by cloning its `src` markets and their order books.

    Gemini already lists `new` but its books were never recorded (it wasn't polled), so each one
    gets a copy of a recorded book.
    """
    tag = lambda key: datetime.fromisoformat(key).replace(tzinfo=timezone.utc).astimezone(
        timezone(timedelta(hours=-5))).strftime("%y%b%d%H").upper()        # e.g. 26NOV2017
    old_tag, new_tag = tag(src), tag(new)
    R = replay.responses
    for key in [k for k in R if "/trade-api/v2/events" in k]:
        clones = []
        for e in R[key]["events"]:
            ms = [dict(m, ticker=m["ticker"].replace(old_tag, new_tag), close_time=new + ":00Z")
                  for m in e["markets"] if expiry_key(m["close_time"]) == src]
            if ms:
                clones.append({"event_ticker": (e.get("event_ticker") or "").replace(old_tag, new_tag), "markets": ms})
        assert clones, f"no Kalshi markets recorded for {src}"
        R[key]["events"] += clones
    for key in [k for k in R if "/orderbook" in k and old_tag in k]:
        R[key.replace(old_tag, new_tag)] = R[key]
    events = next(v for k, v in R.items() if "prediction-markets/events" in k)["data"]
    payloads = [v for k, v in sorted(R.items()) if "/v1/book/" in k]
    symbols = [c["instrumentSymbol"] for e in events for c in e["contracts"] if expiry_key(c["expiryDate"]) == new]
    assert symbols, f"Gemini does not list {new} in the recording"
    for i, sym in enumerate(symbols):
        R[f"api.gemini.com/v1/book/{sym}?limit_asks=10&limit_bids=10"] = payloads[i % len(payloads)]


def ids_for(st, expiry):
    return {cid for v in st.books.values() for cid, b in v.items() if expiry_key(b.expiry) == expiry}


def test_poll_stores_the_match_report(env):
    st = env.poll()
    assert st.match is not None and st.match.matched == st.matched == env.replay.meta["targets"]
    assert len(st.catalog["gemini"]) > len(st.books["gemini"])            # the catalog covers unpolled expiries too
    assert {expiry_key(b.expiry) for b in st.catalog["gemini"]} > set(st.expiries)
    gem_only = [u for u in st.match.unmatched_expiries if u.venue == "gemini"]
    assert [u.expiry for u in gem_only] == [NEW_EXPIRY]
    # every pair's contracts have live books for the polled expiries
    for e in st.expiries:
        for p in st.match.expiries[e].pairs:
            assert p.gemini in st.books["gemini"] and p.kalshi in st.books["kalshi"]
    summary = st.health()["match"]
    assert summary == st.match.summary() and summary["matched_expiries"] == 3 and summary["pairs"] > 0
    assert summary["unmatched_expiries"] >= 1


def test_expired_expiry_rolls_off_and_a_new_one_comes_in(env):
    st = env.poll()
    first, second, third = st.expiries
    old_ids = ids_for(st, first)
    assert old_ids and any(st.history.series(cid) for cid in old_ids)

    # Between polls the clock passes the first expiry: readers stop seeing it at once.
    env.t = to_utc(first) + timedelta(seconds=5)
    assert st.expiries == [first, second, third] and st.live_expiries == [second, third]

    # 1. The next poll rolls it off.
    asked_before = env.replay.count("/orderbook")
    env.poll()
    assert st.expiries == [second, third] and st.matched == [second, third]
    assert ids_for(st, first) == set()                                    # its books are gone
    assert not any(st.history.series(cid) for cid in old_ids)             # and so is its quote history
    assert st.match.expiries[first].expired is True                       # still reported, marked expired
    assert len(st.match.expiries[first].pairs) > 0
    assert st.status() == "ok" and all(s.ok for s in st.sources.values())
    rolled = [k for k in env.replay.hits if "/orderbook" in k]
    assert env.replay.count("/orderbook") > asked_before                  # the live expiries were refreshed
    old_tickers = {cid for cid in old_ids if cid.startswith("KX")}
    assert all(env.replay.hits[k] == 1 for k in rolled if k.split("/markets/")[1].split("/")[0] in old_tickers)

    # 2. Kalshi starts listing an expiry that only Gemini had: it is matched and polled in the same cycle.
    assert NEW_EXPIRY in [u.expiry for u in st.match.unmatched_expiries]
    kalshi_starts_listing(env.replay, third, NEW_EXPIRY)
    env.poll(advance_s=INTERVAL)
    assert st.expiries == [second, third, NEW_EXPIRY]
    assert NEW_EXPIRY not in [u.expiry for u in st.match.unmatched_expiries]
    m = st.match.expiries[NEW_EXPIRY]
    assert m.pairs and m.buckets and not m.expired
    new_books = st.books_for(NEW_EXPIRY)
    assert {b.venue for b in new_books} == {"gemini", "kalshi"}
    assert all(p.gemini in st.books["gemini"] and p.kalshi in st.books["kalshi"] for p in m.pairs)
    assert m.unmatched_on("gemini") and all("Kalshi has no threshold at that strike" in u.reason
                                            for u in m.unmatched_on("gemini"))
    assert st.sources["gemini"].last_error is None and st.status() == "ok"


def test_expiry_rolls_off_even_when_a_listing_is_down(env):
    st = env.poll()
    first, second, third = st.expiries
    env.down(KALSHI)
    env.t = to_utc(first) + timedelta(seconds=5)
    env.poll()
    assert st.expiries == [second, third]                                 # matched from Kalshi's last good listing
    assert st.sources["kalshi"].ok is False and st.match.expiries[first].expired
    assert {expiry_key(b.expiry) for b in st.books["gemini"].values()} == {second, third}


def test_no_expiries_left(env):
    st = env.poll()
    env.t = to_utc(st.matched[-1]) + timedelta(seconds=1)
    env.t = max(env.t, to_utc(NEW_EXPIRY) + timedelta(seconds=1))
    env.poll()
    assert st.expiries == [] and st.matched == [] and st.books == {"gemini": {}, "kalshi": {}}
    assert all(m.expired for m in st.match.expiries.values())
