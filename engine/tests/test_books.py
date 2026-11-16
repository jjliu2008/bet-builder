import pytest

from engine.books import cost_to_buy, expiry_key


def test_walk_yes_asks(mkbook):
    b = mkbook("gemini", 68000, bids=[(0.21, 250)], asks=[(0.26, 100), (0.24, 250)])
    assert b.best_ask == 0.24                       # asks sorted cheapest first
    f = cost_to_buy(b, "YES", 300, round_fees=False)
    assert f.filled == 300 and f.complete
    assert f.levels == [(0.24, 250), (0.26, 50)]
    assert f.avg_price == pytest.approx((0.24 * 250 + 0.26 * 50) / 300)
    assert f.fee == pytest.approx(0.07 * (250 * 0.24 * 0.76 + 50 * 0.26 * 0.74))
    assert f.total == pytest.approx(f.notional + f.fee)


def test_walk_thin_book_partial(mkbook):
    b = mkbook("gemini", 68000, asks=[(0.24, 250)])
    f = cost_to_buy(b, "YES", 1000)
    assert f.filled == 250 and not f.complete


def test_no_side_comes_from_yes_bids(mkbook):
    b = mkbook("kalshi", 68000, bids=[(0.26, 900), (0.25, 100)], asks=[(0.27, 400)])
    no = b.asks("NO")
    assert [l.price for l in no] == [0.74, 0.75]    # buying NO = 1 - YES bid, best first
    f = cost_to_buy(b, "NO", 950)
    assert f.levels == [(0.74, 900), (0.75, 50)]
    assert b.mid == pytest.approx(0.265)


def test_expiry_key():
    assert expiry_key("2026-11-20T22:00:00.000Z") == "2026-11-20T22:00"
    assert expiry_key("2026-11-20T17:00:00-05:00") == "2026-11-20T22:00"
