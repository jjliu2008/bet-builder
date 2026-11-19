"""Reproducible stock-price outcome scenarios with synthetic venue books."""
from datetime import datetime, timezone

from engine.books import Book, Level

STRIKES = {"AAPL": (220, 230), "NVDA": (170, 180)}
AS_OF = datetime(2026, 11, 19, 15, tzinfo=timezone.utc)
EXPIRY = "2026-11-20T21:00"


def books_for(symbol: str) -> list[Book]:
    books = []
    for strike, mid in zip(STRIKES[symbol], (0.64, 0.42)):
        for venue, offset in (("gemini", 0), ("kalshi", 0.03)):
            books.append(Book(
                asset=symbol, venue=venue, strike=strike, expiry=EXPIRY, updated=AS_OF,
                contract_id=f"DEMO-{venue}-{symbol}-{strike}",
                settlement_index=f"Simulated {symbol} closing price",
                yes_bids=[Level(price=mid + offset - 0.02, size=60)],
                yes_asks=[Level(price=mid + offset + 0.02, size=60)],
            ))
    return books
