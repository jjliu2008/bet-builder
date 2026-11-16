import pytest

from engine.fees import round_up_cent
from engine.routes import (RouteConfig, RouteRequest, StrikeNotFound, build_ladder, price_route,
                           tile_buckets)

from .conftest import EXP_KEY, mk


def req(**kw):
    kw.setdefault("expiry", EXP_KEY)
    return RouteRequest(**kw)


def fee1(p):  # rounded fee for a single contract
    return round_up_cent(0.07 * p * (1 - p))


def test_above_picks_cheaper_venue():
    books = [mk("gemini", 68000, asks=[(0.24, 250)]), mk("kalshi", 68000, asks=[(0.27, 400)])]
    r = price_route(req(type="above", lo=68000, size=1), books)
    assert r.build == "single"
    assert [l.venue for l in r.legs] == ["gemini"]
    assert r.legs[0].side == "YES"
    assert r.total_cost == pytest.approx(0.24 + fee1(0.24))
    assert r.best_single_venue_cost == pytest.approx(r.total_cost)
    assert r.savings == pytest.approx(0)
    assert r.mixed_settlement_index is False


def test_below_buys_no_at_one_minus_bid():
    books = [mk("gemini", 68000, bids=[(0.21, 250)]), mk("kalshi", 68000, bids=[(0.26, 900)])]
    r = price_route(req(type="below", hi=68000), books)
    assert r.legs[0].venue == "kalshi" and r.legs[0].side == "NO"
    assert r.legs[0].price == pytest.approx(0.74)
    assert r.legs[0].strike_hi == 68000 and r.legs[0].strike_lo is None


def test_venue_split_when_book_thin():
    books = [mk("gemini", 68000, asks=[(0.40, 100)]), mk("kalshi", 68000, asks=[(0.42, 1000)])]
    r = price_route(req(type="above", lo=68000, size=300), books)
    sizes = {l.venue: l.size for l in r.legs}
    assert sizes == {"gemini": 100, "kalshi": 200}
    assert r.filled_size == 300 and r.mixed_settlement_index
    kalshi_only = (0.42 * 300 + round_up_cent(0.07 * 300 * 0.42 * 0.58)) / 300
    assert r.best_single_venue_cost == pytest.approx(kalshi_only)
    assert r.savings == pytest.approx(kalshi_only - r.total_cost, abs=1e-5) and r.savings > 0


def test_partial_fill_warns():
    books = [mk("gemini", 68000, asks=[(0.40, 100)])]
    r = price_route(req(type="above", lo=68000, size=500), books)
    assert r.filled_size == 100 and r.warnings
    assert r.best_single_venue_cost is None and r.savings is None


def test_two_leg_math():
    books = [
        mk("gemini", 66000, asks=[(0.60, 500)]), mk("kalshi", 66000, asks=[(0.63, 500)]),
        mk("gemini", 68000, bids=[(0.25, 500)]), mk("kalshi", 68000, bids=[(0.30, 500)]),
    ]
    r = price_route(req(type="between", lo=66000, hi=68000, size=1), books)
    assert r.build == "two_leg"
    yes, no = r.legs
    assert (yes.venue, yes.side, yes.price) == ("gemini", "YES", 0.60)
    assert (no.venue, no.side) == ("kalshi", "NO") and no.price == pytest.approx(0.70)
    cash = 0.60 + fee1(0.60) + 0.70 + fee1(0.70)
    assert r.cash_outlay == pytest.approx(cash)
    assert r.total_cost == pytest.approx(cash - 1.0)       # pays $2 in range, $1 outside
    assert r.guaranteed_payout == 1.0 and r.max_payout == 1.0
    assert r.mixed_settlement_index is True
    # single venue: gemini 0.60+0.75, kalshi 0.63+0.70 -> kalshi is cheaper
    k_only = 0.63 + fee1(0.63) + 0.70 + fee1(0.70) - 1
    assert r.best_single_venue_cost == pytest.approx(k_only)
    assert r.savings == pytest.approx(k_only - r.total_cost, abs=1e-5)


def _buckets(prices):
    out, lo = [], 66000
    for p in prices:
        out.append(mk("kalshi", lo, asks=[(p, 1000)], kind="bucket", lo=lo, hi=lo + 500))
        lo += 500
    return out


def test_bucket_tiling_beats_two_leg_when_cheaper():
    books = _buckets([0.10, 0.12, 0.11, 0.09]) + [
        mk("gemini", 66000, asks=[(0.80, 100)]), mk("gemini", 68000, bids=[(0.10, 100)]),
    ]
    r = price_route(req(type="between", lo=66000, hi=68000), books)
    assert r.build == "buckets" and len(r.legs) == 4
    expected = sum(p + fee1(p) for p in [0.10, 0.12, 0.11, 0.09])
    assert r.total_cost == pytest.approx(expected)
    assert r.guaranteed_payout == 0.0
    builds = {a.build for a in r.alternatives}
    assert builds == {"buckets", "two_leg"}


def test_tile_buckets_gap_and_partial_range():
    bs = _buckets([0.1, 0.1, 0.1])            # 66000..67500
    assert [b.lo for b in tile_buckets(bs, 66500, 67500)] == [66500, 67000]
    assert tile_buckets(bs, 66000, 68000) is None          # runs out
    gap = [bs[0], bs[2]]
    assert tile_buckets(gap, 66000, 67500) is None
    tail = mk("kalshi", None, asks=[(0.05, 10)], kind="bucket", lo=None, hi=66000)
    assert tile_buckets(bs + [tail], None, 67000)[0] is tail


def test_missing_strike_lists_nearest():
    books = [mk("gemini", 68000, asks=[(0.2, 1)]), mk("kalshi", 68500, asks=[(0.2, 1)])]
    with pytest.raises(StrikeNotFound) as e:
        price_route(req(type="above", lo=68100), books)
    assert e.value.nearest == [68000, 68500]
    assert "68100" in str(e.value) and "68000" in str(e.value)


def test_other_expiries_ignored():
    books = [mk("gemini", 68000, asks=[(0.2, 1)])]
    with pytest.raises(ValueError):
        price_route(req(type="above", lo=68000, expiry="2026-11-21T22:00"), books)


def test_fair_and_stale_hooks_and_verdict():
    from datetime import timedelta
    from .conftest import EXP
    books = [mk("gemini", 68000, asks=[(0.24, 250)])]
    t = EXP - timedelta(days=1)
    r = price_route(req(type="above", lo=68000), books, now=t,
                    fair_fn=lambda rq: 0.20, stale_fn=lambda b: "quote frozen")
    assert r.fair_value == 0.20 and r.verdict == "overpriced"   # 0.26 vs 0.20
    assert r.stale_warnings and "quote frozen" in r.stale_warnings[0]
    r2 = price_route(req(type="above", lo=68000), books, fair_fn=lambda rq: 0.30, now=t)
    assert r2.verdict == "good deal"
    r3 = price_route(req(type="above", lo=68000), books, fair_fn=lambda rq: 0.25, now=t)
    assert r3.verdict == "fair"


def test_unrounded_fees_flag():
    books = [mk("gemini", 68000, asks=[(0.24, 250)])]
    r = price_route(req(type="above", lo=68000), books, config=RouteConfig(round_fees=False))
    assert r.total_cost == pytest.approx(0.24 + 0.07 * 0.24 * 0.76)


def test_request_validation():
    with pytest.raises(ValueError):
        RouteRequest(expiry=EXP_KEY, type="between", lo=68000, hi=66000)
    assert RouteRequest(expiry=EXP_KEY, type="below", lo=68000).strike == 68000


def test_ladder_shape():
    books = [mk("gemini", 68000, bids=[(0.21, 250)], asks=[(0.24, 250)]),
             mk("kalshi", 68000, bids=[(0.26, 900)], asks=[(0.27, 400)]),
             mk("kalshi", 68500, bids=[(0.1, 5)], asks=[(0.12, 5)])]
    rows = build_ladder(books, "BTC", EXP_KEY, fair={68000: 0.23},
                        stale={68000: {"kalshi": "too far from fair"}})
    assert [r["strike"] for r in rows] == [68000, 68500]
    r = rows[0]
    assert r["gemini"]["bid"] == 0.21 and r["kalshi"]["ask_size"] == 400
    assert r["fair"] == 0.23 and r["stale"] == {"gemini": False, "kalshi": True}
    assert rows[1]["gemini"] is None


def test_warns_when_cost_exceeds_payout():
    books = _buckets([0.40, 0.40, 0.40])
    r = price_route(req(type="between", lo=66000, hi=67500), books)
    assert r.total_cost > 1 and any("cannot profit" in w for w in r.warnings)


def test_fair_hidden_close_to_expiry():
    from datetime import timedelta
    from .conftest import EXP
    books = [mk("gemini", 68000, asks=[(0.24, 250)])]
    called = []
    fair_fn = lambda rq: called.append(1) or 0.20
    near = price_route(req(type="above", lo=68000), books, fair_fn=fair_fn,
                       now=EXP - timedelta(hours=1, minutes=59))
    assert near.fair_value is None and near.verdict == "n/a (too close to expiry)" and not called
    far = price_route(req(type="above", lo=68000), books, fair_fn=fair_fn,
                      now=EXP - timedelta(hours=2, minutes=1))
    assert far.fair_value == 0.20 and far.verdict == "overpriced"
    rows = build_ladder(books, "BTC", EXP_KEY, fair={68000: 0.2}, now=EXP - timedelta(minutes=30))
    assert rows[0]["fair"] is None
    rows = build_ladder(books, "BTC", EXP_KEY, fair={68000: 0.2}, now=EXP - timedelta(hours=3))
    assert rows[0]["fair"] == 0.2
