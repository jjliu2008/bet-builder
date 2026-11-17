"""Kalshi adapter. Public endpoints, no key. Parsing is `engine.dev_fetch`'s.

The event listing already carries each market's top of book, so it costs no extra requests.
Full depth is one request per market, so the poller asks for it only near spot.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from engine import Book, expiry_key
from engine.dev_fetch import _kalshi_top, kalshi_market_to_book, parse_kalshi_orderbook

from .http import VenueClient

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MAX_PAGES = 10
Market = Tuple[str, dict]             # (series ticker, market)


def make_client(rps: float = 12.0, **kw) -> VenueClient:
    return VenueClient("kalshi", BASE_URL, rps, **kw)


def threshold_series(asset: str) -> str:
    """The 'above K' ladder series, e.g. KXBTCD."""
    return f"KX{asset.upper()}D"


def bucket_series(asset: str) -> str:
    """The range-bucket series, e.g. KXBTC."""
    return f"KX{asset.upper()}"


def expiry_of(market: dict) -> str:
    return expiry_key(market["close_time"])


async def list_markets(client: VenueClient, asset: str = "BTC") -> List[Market]:
    """Return every open threshold and bucket market for `asset`, each with its series ticker."""
    out: List[Market] = []
    for series in (threshold_series(asset), bucket_series(asset)):
        cursor = None
        for _ in range(MAX_PAGES):
            params = {"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = await client.get("/events", params)
            events = d.get("events", [])
            out += [(series, m) for e in events for m in e.get("markets", [])]
            cursor = d.get("cursor")
            if not cursor or not events:
                break
    return out


def in_window(market: dict, window: Optional[Tuple[float, float]]) -> bool:
    """Return True if the market's strike is inside the ($lo, $hi) window. No window means no.

    A one-sided market ("above K" or "below K") counts by its single strike. Treating its open
    side as infinite would put every strike below the window inside it. A bucket counts if its
    range overlaps the window.
    """
    if window is None:
        return False
    floor, cap = market.get("floor_strike"), market.get("cap_strike")
    if floor is None and cap is None:
        return False
    lo = floor if floor is not None else cap
    hi = cap if cap is not None else floor
    return hi >= window[0] and lo <= window[1]


def top_book(series: str, market: dict, asset: str = "BTC", now: Optional[datetime] = None) -> Optional[Book]:
    """Build a one-level Book from the listing's best bid and ask. No request is made."""
    bids, asks = _kalshi_top(market)
    return kalshi_market_to_book(market, series, bids, asks, asset, now)


async def fetch_book(client: VenueClient, series: str, market: dict, asset: str = "BTC",
                     now: Optional[datetime] = None) -> Optional[Book]:
    """Fetch one market's top 10 levels per side. YES ask = 1 - NO bid. None for unknown strike types."""
    d = await client.get(f"/markets/{market['ticker']}/orderbook", {"depth": 10})
    bids, asks = parse_kalshi_orderbook(d)
    return kalshi_market_to_book(market, series, bids, asks, asset, now)
