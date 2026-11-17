"""Gemini prediction-market adapter. Public endpoints, no key. Parsing is `engine.dev_fetch`'s."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from engine import Book, expiry_key
from engine.dev_fetch import parse_gemini_book

from .http import VenueClient

BASE_URL = "https://api.gemini.com"
Contract = Tuple[dict, dict]          # (contract, its event)


def make_client(rps: float = 5.0, **kw) -> VenueClient:
    return VenueClient("gemini", BASE_URL, rps, **kw)


def expiry_of(contract: dict, event: dict) -> str:
    """Return the contract's expiry key, e.g. '2026-11-20T22:00'."""
    return expiry_key(contract.get("expiryDate") or event["expiryDate"])


async def list_contracts(client: VenueClient, asset: str = "BTC") -> List[Contract]:
    """Return every active 'above' strike contract for `asset`, each with its event.

    Short-interval Up/Down events (BTC05M, BTC15M) don't match the ticker pattern and are skipped.
    """
    pat = re.compile(rf"^{asset.upper()}\d{{10}}$")            # e.g. BTC2611202200
    out: List[Contract] = []
    offset = 0
    while True:
        d = await client.get("/v1/prediction-markets/events",
                             {"status": "active", "category": "Crypto", "limit": 100, "offset": offset})
        data = d.get("data", [])
        for e in data:
            if pat.match(e.get("ticker", "")):
                out += [(c, e) for c in e.get("contracts", []) if (c.get("strike") or {}).get("type") == "above"]
        offset += len(data)
        if not data or offset >= int(d.get("pagination", {}).get("total", 0)):
            return out


async def fetch_book(client: VenueClient, contract: dict, event: dict, asset: str = "BTC",
                     now: Optional[datetime] = None) -> Book:
    """Fetch one contract's top 10 levels per side and normalize them into a threshold Book."""
    bj = await client.get(f"/v1/book/{contract['instrumentSymbol']}", {"limit_bids": 10, "limit_asks": 10})
    return parse_gemini_book(contract, event, bj, asset, now)


async def fetch_books(client: VenueClient, contracts: Iterable[Contract], asset: str = "BTC",
                      now: Optional[datetime] = None) -> List[object]:
    """Fetch several books at once. Each result is a Book, or the Exception that request raised."""
    return await asyncio.gather(*(fetch_book(client, c, e, asset, now) for c, e in contracts),
                                return_exceptions=True)
