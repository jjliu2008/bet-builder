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


@router.get("/spot/{symbol}")
def spot_quote(symbol: str):
    stocks = {"AAPL", "NVDA", "MSFT", "TSLA"}
    crypto = {"ETH", "SOL"}
    if symbol not in stocks | crypto:
        raise HTTPException(404, "Unknown spot symbol")
    try:
        if symbol in stocks:
            response = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                                    params={"range": "5d", "interval": "1d"},
                                    headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            response.raise_for_status()
            meta = response.json()["chart"]["result"][0]["meta"]
            if meta["symbol"] != symbol or meta["currency"] != "USD" or meta["instrumentType"] != "EQUITY":
                raise ValueError("Unexpected instrument")
            price = float(meta["regularMarketPrice"])
            timestamp = datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc)
            source = "Yahoo Finance · regular session"
        else:
            response = requests.get(f"https://api.exchange.coinbase.com/products/{symbol}-USD/ticker", timeout=8)
            response.raise_for_status()
            ticker = response.json()
            price = float(ticker["price"])
            timestamp = datetime.fromisoformat(ticker["time"].replace("Z", "+00:00"))
            source = "Coinbase Exchange"
        if not isfinite(price) or price <= 0 or timestamp.tzinfo is None or timestamp > datetime.now(timezone.utc):
            raise ValueError("Invalid market observation")
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError, OverflowError, OSError):
        raise HTTPException(503, f"Latest USD price unavailable for {symbol}") from None
    return {"symbol": symbol, "price_usd": price, "traded_at": timestamp, "source": source}
