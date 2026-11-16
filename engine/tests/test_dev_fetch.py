"""Offline tests for venue parsing in dev_fetch, run against captured response shapes."""
from engine.dev_fetch import _edge, kalshi_market_to_book, parse_gemini_book, parse_kalshi_orderbook


def test_edge_mapping():
    assert _edge(67999.99) == 68000
    assert _edge(57000) == 57000
    assert _edge(None) is None


def test_kalshi_orderbook_dollars_and_cents():
    new = {"orderbook_fp": {"yes_dollars": [["0.2400", "4072.00"], ["0.2600", "900"]],
                            "no_dollars": [["0.7200", "100"], ["0.7300", "400.5"]]}}
    old = {"orderbook": {"yes": [[24, 4072], [26, 900]], "no": [[72, 100], [73, 400.5]]}}
    for d in (new, old):
        bids, asks = parse_kalshi_orderbook(d)
        assert [(l.price, l.size) for l in bids] == [(0.24, 4072), (0.26, 900)]
        assert sorted((l.price, l.size) for l in asks) == [(0.27, 400.5), (0.28, 100)]


def _m(**kw):
    base = {"ticker": "T", "close_time": "2026-11-20T22:00:00Z", "floor_strike": None, "cap_strike": None}
    base.update(kw)
    return base


def test_kalshi_market_kinds():
    t = kalshi_market_to_book(_m(strike_type="greater", floor_strike=67999.99), "KXBTCD", [], [])
    assert (t.kind, t.strike, t.lo, t.hi) == ("threshold", 68000, 68000, None)
    b = kalshi_market_to_book(_m(strike_type="between", floor_strike=65000, cap_strike=65499.99), "KXBTC", [], [])
    assert (b.kind, b.lo, b.hi) == ("bucket", 65000, 65500)
    lo = kalshi_market_to_book(_m(strike_type="less", cap_strike=57000), "KXBTC", [], [])
    assert (lo.lo, lo.hi) == (None, 57000)
    hi = kalshi_market_to_book(_m(strike_type="greater", floor_strike=80999.99), "KXBTC", [], [])
    assert (hi.kind, hi.lo, hi.hi) == ("bucket", 81000, None)


def test_gemini_book_parse():
    c = {"instrumentSymbol": "GEMI-BTC2611202200-HI65000", "expiryDate": "2026-11-20T22:00:00.000Z",
         "strike": {"type": "above", "value": "65000"}}
    e = {"slug": "btc-price-on-november-20", "expiryDate": "2026-11-20T22:00:00.000Z"}
    bj = {"bids": [{"price": "0.5", "amount": "250.0", "timestamp": "1794864102"}],
          "asks": [{"price": "0.55", "amount": "1111.0", "timestamp": "1794864102"},
                   {"price": "0.54", "amount": "250.0", "timestamp": "1794864102"}]}
    b = parse_gemini_book(c, e, bj)
    assert b.strike == 65000 and b.best_ask == 0.54 and b.best_bid == 0.5
    assert b.link.endswith("/predictions/btc-price-on-november-20")
