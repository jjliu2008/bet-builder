from datetime import datetime, timedelta, timezone

from engine.books import Book, Level
from engine.routes import RouteRequest, build_ladder, price_route
from engine.stale import History, StaleConfig, check_quote, make_stale_fn, stale_flags

from .conftest import EXP, EXP_KEY

NOW = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)


def bk(venue, mid, strike=68000, updated=NOW):
    return Book(venue=venue, contract_id=f"{venue}-{strike}", strike=strike, expiry=EXP,
                yes_bids=[Level(price=round(mid - 0.01, 4), size=100)],
                yes_asks=[Level(price=round(mid + 0.01, 4), size=100)], updated=updated)


def flat_history(cid, mid, minutes):
    return {cid: [(NOW - timedelta(minutes=m), mid) for m in range(minutes, -1, -1)]}


def spot(move_bp):
    s0 = 65000.0
    return [(NOW - timedelta(minutes=6), s0), (NOW - timedelta(minutes=2), s0 * (1 + move_bp / 1e4)),
            (NOW, s0 * (1 + move_bp / 1e4))]


def test_small_gap_never_flags():
    g, k = bk("gemini", 0.30), bk("kalshi", 0.27)
    assert check_quote(g, k, fair=0.27, quote_history=flat_history(g.contract_id, 0.30, 10),
                       spot_history=spot(100), now=NOW) is None


def test_frozen_while_btc_moved():
    g, k = bk("gemini", 0.30), bk("kalshi", 0.20)
    r = check_quote(g, k, quote_history=flat_history(g.contract_id, 0.30, 6),
                    spot_history=spot(45), now=NOW)
    assert r and "hasn't changed in 5 min" in r and "45 bp" in r and "10.0¢" in r


def test_frozen_but_btc_quiet_not_flagged():
    g, k = bk("gemini", 0.30), bk("kalshi", 0.20)
    assert check_quote(g, k, quote_history=flat_history(g.contract_id, 0.30, 6),
                       spot_history=spot(10), now=NOW) is None


def test_not_enough_history_not_frozen():
    g, k = bk("gemini", 0.30), bk("kalshi", 0.20)
    assert check_quote(g, k, quote_history=flat_history(g.contract_id, 0.30, 3),
                       spot_history=spot(45), now=NOW) is None


def test_mid_changed_recently_not_frozen():
    g, k = bk("gemini", 0.30), bk("kalshi", 0.20)
    h = flat_history(g.contract_id, 0.30, 6)
    h[g.contract_id][-3] = (h[g.contract_id][-3][0], 0.31)
    assert check_quote(g, k, quote_history=h, spot_history=spot(45), now=NOW) is None


def test_fallback_to_updated_timestamp():
    g = bk("gemini", 0.30, updated=NOW - timedelta(minutes=8))
    k = bk("kalshi", 0.20)
    assert "hasn't changed" in check_quote(g, k, spot_history=spot(45), now=NOW)


def test_off_fair_rule():
    g, k = bk("gemini", 0.30), bk("kalshi", 0.21)
    r = check_quote(g, k, fair=0.22, now=NOW)
    assert r and "Kalshi is within 2¢ of fair 0.220" in r
    assert check_quote(k, g, fair=0.22, now=NOW) is None          # the venue near fair isn't flagged
    assert check_quote(g, k, fair=0.17, now=NOW) is None          # neither within 2c of fair
    loose = StaleConfig(fair_band=0.05)
    assert check_quote(g, k, fair=0.17, now=NOW, config=loose) is not None


def test_flags_ladder_and_route_integration():
    g, k = bk("gemini", 0.30), bk("kalshi", 0.21)
    other = bk("kalshi", 0.10, strike=68500)
    books = [g, k, other]
    flags = stale_flags(books, EXP_KEY, fair={68000: 0.22}, now=NOW)
    assert flags[68000]["gemini"] and flags[68000]["kalshi"] is None
    assert flags[68500] == {"kalshi": None}
    rows = build_ladder(books, "BTC", EXP_KEY, stale=flags)
    assert rows[0]["stale"] == {"gemini": True, "kalshi": False}
    fn = make_stale_fn(books, EXP_KEY, fair={68000: 0.22}, now=NOW)
    r = price_route(RouteRequest(expiry=EXP_KEY, type="above", lo=68000), [g], stale_fn=fn)
    assert r.stale_warnings and r.stale_warnings[0].startswith("gemini gemini-68000: Gemini mid")


def test_history_store_prunes():
    h = History(max_age_s=600)
    h.record("x", 0.3, NOW - timedelta(minutes=20))
    h.record("x", 0.31, NOW)
    assert h.series("x") == [(NOW, 0.31)]
    h.record_spot("btc", 65000, NOW)
    assert "spot:BTC" in h.as_mapping()
