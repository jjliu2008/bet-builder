from datetime import datetime, timezone

import pytest

from engine.books import Book, Level

EXP = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)
EXP_KEY = "2026-11-20T22:00"


def mk(venue, strike, bids=(), asks=(), kind="threshold", lo=None, hi=None, cid=None):
    """Build a fixture book. bids/asks are (price, size) pairs."""
    return Book(
        venue=venue, contract_id=cid or f"{venue}-{kind}-{strike}-{lo}-{hi}", strike=strike,
        expiry=EXP, kind=kind, lo=lo, hi=hi,
        yes_bids=[Level(price=p, size=s) for p, s in bids],
        yes_asks=[Level(price=p, size=s) for p, s in asks],
        settlement_index="Gemini BTC index" if venue == "gemini" else "CF Benchmarks BRTI 60s avg",
        link=f"https://example/{venue}",
    )


@pytest.fixture
def mkbook():
    return mk
