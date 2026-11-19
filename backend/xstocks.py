"""Read-only reference prices for issuer-listed tokenized equities."""
from datetime import datetime, timezone
from math import isfinite

import requests
from fastapi import APIRouter, HTTPException

router = APIRouter()
PRODUCTS = {"AAPLx": "apple-xstock", "NVDAx": "nvidia-xstock"}


@router.get("/rwas/{symbol}")
def last_trade(symbol: str):
    if symbol not in PRODUCTS:
        raise HTTPException(404, "Unknown tokenized equity")
    pair = f"{symbol}/USD"
    try:
        response = requests.get("https://api.kraken.com/0/public/Trades", params={
            "pair": pair, "assetVersion": 1, "asset_class": "tokenized_asset", "count": 1,
        }, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data["error"]:
            raise ValueError("Venue error")
        trade = data["result"][pair][-1]
        price = float(trade[0])
        timestamp = datetime.fromtimestamp(float(trade[2]), timezone.utc)
        if not isfinite(price) or price <= 0 or timestamp > datetime.now(timezone.utc):
            raise ValueError("Invalid market observation")
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError, OverflowError, OSError):
        raise HTTPException(503, f"Kraken last trade unavailable for {pair}") from None
    return {"symbol": symbol, "price_usd": price, "traded_at": timestamp,
            "source": "Kraken", "issuer_url": f"https://assets.backed.fi/products/{PRODUCTS[symbol]}"}
