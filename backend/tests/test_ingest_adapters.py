"""Offline tests for the Gemini, Kalshi, and Deribit adapters, using recorded live responses."""
import asyncio
import re

import pytest

from backend.ingest import deribit, gemini, kalshi
from backend.tests.replay import Replay
from engine import build_surface, fair_above
from engine.dev_fetch import _levels


@pytest.fixture
def replay():
    return Replay()


def run(coro):
    return asyncio.run(coro)


def clients(replay):
    kw = dict(transport=replay.transport)
    return gemini.make_client(1e6, **kw), kalshi.make_client(1e6, **kw), deribit.make_client(1e6, **kw)


def window(replay, pct=0.03):
    spot = replay.meta["spot"]
    return (spot * (1 - pct), spot * (1 + pct))


def sorted_prices(levels, reverse):
    p = [l.price for l in levels]
    return p == sorted(p, reverse=reverse)


# ---- Gemini ------------------------------------------------------------------------------
def test_gemini_listing_keeps_only_strike_ladders(replay):
    g, _, _ = clients(replay)
    contracts = run(gemini.list_contracts(g, "BTC"))
    assert contracts
    raw = next(v for k, v in replay.responses.items() if "prediction-markets/events" in k)
    assert any(not re.match(r"^BTC\d{10}$", e["ticker"]) for e in raw["data"])     # the recording has others
    for c, e in contracts:
        assert re.match(r"^BTC\d{10}$", e["ticker"])
        assert c["strike"]["type"] == "above"
    expiries = {gemini.expiry_of(c, e) for c, e in contracts}
    assert set(replay.meta["targets"]) <= expiries


def test_gemini_books_are_normalized(replay):
    g, _, _ = clients(replay)
    contracts = run(gemini.list_contracts(g, "BTC"))
    target = replay.meta["targets"][0]
    jobs = [(c, e) for c, e in contracts if gemini.expiry_of(c, e) == target]
    books = run(gemini.fetch_books(g, jobs, "BTC", replay.now))
    assert books and not any(isinstance(b, Exception) for b in books)
    assert any(b.yes_bids and b.yes_asks for b in books)
    for (c, e), b in zip(jobs, books):
        assert (b.venue, b.kind, b.asset) == ("gemini", "threshold", "BTC")
        assert b.contract_id == c["instrumentSymbol"]
        assert b.strike == b.lo == float(c["strike"]["value"]) and b.hi is None
        assert gemini.expiry_of(c, e) == target
        assert all(0 < l.price < 1 and l.size > 0 for l in b.yes_bids + b.yes_asks)
        assert sorted_prices(b.yes_bids, reverse=True) and sorted_prices(b.yes_asks, reverse=False)
        if b.best_bid is not None and b.best_ask is not None:
            assert b.best_bid < b.best_ask
        raw = next(v for k, v in replay.responses.items() if k.split("?")[0].endswith("/v1/book/" + b.contract_id))
        assert len(b.yes_bids) == sum(float(l["amount"]) > 0 for l in raw["bids"])
        if raw["bids"]:
            assert b.best_bid == max(float(l["price"]) for l in raw["bids"] if float(l["amount"]) > 0)
        assert b.link.startswith("https://www.gemini.com/predictions/")


def test_gemini_book_error_is_returned_not_raised(replay):
    g, _, _ = clients(replay)
    contracts = run(gemini.list_contracts(g, "BTC"))
    ghost = ({**contracts[0][0], "instrumentSymbol": "GEMI-NOT-RECORDED"}, contracts[0][1])
    results = run(gemini.fetch_books(g, [contracts[0], ghost], "BTC", replay.now))
    assert not isinstance(results[0], Exception) and isinstance(results[1], Exception)


# ---- Kalshi ------------------------------------------------------------------------------
def test_kalshi_listing_has_both_series(replay):
    _, k, _ = clients(replay)
    markets = run(kalshi.list_markets(k, "BTC"))
    series = {s for s, _ in markets}
    assert series == {"KXBTCD", "KXBTC"}
    assert all(m["strike_type"] == "greater" for s, m in markets if s == "KXBTCD")
    assert set(replay.meta["targets"]) <= {kalshi.expiry_of(m) for s, m in markets if s == "KXBTCD"}
    assert replay.count("/events") == 2                                           # one page per series


def _recorded_orderbook(replay, ticker):
    raw = replay.responses.get(f"api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook?depth=10")
    if raw is None:
        return None
    ob = raw.get("orderbook_fp") or raw.get("orderbook") or {}
    return _levels(ob.get("yes_dollars") or ob.get("yes")), _levels(ob.get("no_dollars") or ob.get("no"))


def test_kalshi_threshold_books_full_depth(replay):
    _, k, _ = clients(replay)
    markets = run(kalshi.list_markets(k, "BTC"))
    target, win = replay.meta["targets"][0], window(replay)
    jobs = [(s, m) for s, m in markets
            if s == "KXBTCD" and kalshi.expiry_of(m) == target and kalshi.in_window(m, win)]
    assert jobs
    async def fetch_all():
        return await asyncio.gather(*(kalshi.fetch_book(k, s, m, "BTC", replay.now) for s, m in jobs))

    books = run(fetch_all())
    deep = 0
    for (s, m), b in zip(jobs, books):
        assert (b.venue, b.kind, b.contract_id) == ("kalshi", "threshold", m["ticker"])
        # 64999.99 stands for "at or above 65000"
        assert b.strike == b.lo == pytest.approx(round(m["floor_strike"] + 0.01, 2)) and b.hi is None
        assert b.strike == round(b.strike)
        yes, no = _recorded_orderbook(replay, m["ticker"])
        assert len(b.yes_bids) == len(yes) and len(b.yes_asks) == len(no)
        if yes:
            assert b.best_bid == pytest.approx(max(p for p, _ in yes))
        if no:                                               # a NO bid at q is a YES ask at 1 - q
            assert b.best_ask == pytest.approx(1 - max(q for q, _ in no))
            assert sorted(l.price for l in b.yes_asks) == pytest.approx(sorted(1 - q for q, _ in no))
        assert all(0 < l.price < 1 and l.size > 0 for l in b.yes_bids + b.yes_asks)
        assert sorted_prices(b.yes_bids, reverse=True) and sorted_prices(b.yes_asks, reverse=False)
        if b.best_bid is not None and b.best_ask is not None:
            assert b.best_bid < b.best_ask
        deep += len(b.yes_bids) > 1 or len(b.yes_asks) > 1
    assert deep > 0                                          # full depth really has more than one level


def test_kalshi_bucket_bounds(replay):
    _, k, _ = clients(replay)
    markets = run(kalshi.list_markets(k, "BTC"))
    between = [(s, m) for s, m in markets if s == "KXBTC" and m["strike_type"] == "between"]
    assert between
    for s, m in between[:20]:
        b = kalshi.top_book(s, m, "BTC", replay.now)
        assert b.kind == "bucket"
        assert b.lo == pytest.approx(m["floor_strike"])
        assert b.hi == pytest.approx(round(m["cap_strike"] + 0.01, 2))             # [floor, cap + 0.01)
        assert b.lo < b.hi


def test_kalshi_top_of_book_makes_no_request(replay):
    _, k, _ = clients(replay)
    markets = run(kalshi.list_markets(k, "BTC"))
    before = sum(replay.hits.values())
    books = [kalshi.top_book(s, m, "BTC", replay.now) for s, m in markets]
    assert sum(replay.hits.values()) == before
    assert all(b is not None and len(b.yes_bids) <= 1 and len(b.yes_asks) <= 1 for b in books)
    s, m = next((s, m) for s, m in markets if float(m["yes_bid_dollars"] or 0) > 0 and float(m["yes_bid_size_fp"] or 0) > 0)
    b = kalshi.top_book(s, m, "BTC", replay.now)
    assert b.best_bid == pytest.approx(float(m["yes_bid_dollars"]))
    assert b.yes_bids[0].size == pytest.approx(float(m["yes_bid_size_fp"]))


def test_kalshi_in_window():
    above = {"floor_strike": 64999.99, "cap_strike": None}                         # "at or above 65000"
    assert kalshi.in_window(above, (63000, 67000))
    assert not kalshi.in_window(above, (65500, 67000))       # its open upper side doesn't count
    assert not kalshi.in_window({"floor_strike": 54599.99, "cap_strike": None}, (63000, 67000))
    below = {"floor_strike": None, "cap_strike": 57000}
    assert not kalshi.in_window(below, (63000, 67000)) and kalshi.in_window(below, (56000, 58000))
    assert not kalshi.in_window({"floor_strike": 70000, "cap_strike": 70499.99}, (63000, 67000))
    assert kalshi.in_window({"floor_strike": 66900, "cap_strike": 67399.99}, (63000, 67000))   # overlaps
    assert not kalshi.in_window({"floor_strike": None, "cap_strike": None}, (63000, 67000))
    assert not kalshi.in_window(above, None)                                       # spot unknown: top of book


# ---- Deribit -----------------------------------------------------------------------------
def test_deribit_spot_and_surface(replay):
    _, _, d = clients(replay)
    spot = run(deribit.fetch_spot(d, "BTC"))
    assert spot == replay.meta["spot"] and 1_000 < spot < 10_000_000
    rows = run(deribit.fetch_rows(d, "BTC"))
    assert rows and {"instrument_name", "mark_iv", "underlying_price"} <= set(rows[0])
    surface = build_surface(rows, "BTC", replay.now)
    assert len(surface.slices) >= 2
    step = 500
    base = round(spot / step) * step
    fair = fair_above("BTC", replay.meta["targets"][-1], [base + i * step for i in range(-4, 5)], surface=surface)
    p = list(fair.values())
    assert all(0 <= x <= 1 for x in p) and p == sorted(p, reverse=True)
    assert p[0] > 0.5 > p[-1]                                # below spot is likelier than not; above is not
