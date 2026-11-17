"""Deribit adapter: the index price (spot) and the option summaries that feed the fair-value surface."""
from __future__ import annotations

from typing import List

from .http import VenueClient

BASE_URL = "https://www.deribit.com/api/v2/public"


def make_client(rps: float = 5.0, **kw) -> VenueClient:
    return VenueClient("deribit", BASE_URL, rps, **kw)


async def fetch_spot(client: VenueClient, asset: str = "BTC") -> float:
    """Return the Deribit index price for `asset`, in $."""
    d = await client.get("/get_index_price", {"index_name": f"{asset.lower()}_usd"})
    return float(d["result"]["index_price"])


async def fetch_rows(client: VenueClient, asset: str = "BTC") -> List[dict]:
    """Return Deribit's option book summaries, the input of `engine.build_surface`."""
    d = await client.get("/get_book_summary_by_currency", {"currency": asset.upper(), "kind": "option"})
    return d["result"]
