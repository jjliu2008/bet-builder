"""Read-through catalog and visible order books for non-BTC prediction routes."""
import asyncio
import json
import re
from datetime import datetime, timezone
from time import monotonic

import httpx
import requests
from fastapi import HTTPException

from engine.books import Book, Level, expiry_key, to_utc
from engine.dev_fetch import _kalshi_top
from .ingest.kalshi import BASE_URL
from .ingest import gemini
from engine.fairvalue import build_surface
from engine.stale import History

EQUITIES = {"AAPL", "NVDA", "MSFT", "TSLA"}
CRYPTO = {"ETH": "KXETHD", "SOL": "KXSOLD"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def fetch(url, **kwargs):
    r = requests.get(url, timeout=10, **kwargs)
    r.raise_for_status()
    return r.json()


async def gemini_snapshot(asset, contracts=None):
    client = gemini.make_client(retries=1)
    try:
        if contracts is None:
            return await gemini.list_contracts(client, asset)
        results = await gemini.fetch_books(client, contracts, asset)
        return [b.model_copy(update={"settlement_index": c.get("source") or ""})
                for b, (c, _) in zip(results, contracts) if isinstance(b, Book)]
    finally:
        await client.aclose()


def eth_surface(asset):
    rows = fetch("https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
                 params={"currency": asset, "kind": "option"})["result"]
    return build_surface(rows, asset, fit_smiles=False)


def catalog(asset):
    now = datetime.now(timezone.utc)
    if asset in CRYPTO:
        out = {}
        failures = 0
        try:
            events = fetch(BASE_URL + "/events", params={"series_ticker": CRYPTO[asset], "status": "open",
                           "limit": 100, "with_nested_markets": "true"})["events"]
            for event in events:
                for m in event.get("markets", [])[:1]:
                    if to_utc(m["close_time"]) > now:
                        out.setdefault(expiry_key(m["close_time"]), {})["kalshi"] = event["event_ticker"]
        except requests.RequestException:
            failures += 1
        try:
            for contract, event in asyncio.run(gemini_snapshot(asset)):
                key = gemini.expiry_of(contract, event)
                if to_utc(key) > now:
                    out.setdefault(key, {}).setdefault("gemini", []).append((contract, event))
        except (OSError, RuntimeError, ValueError, httpx.HTTPError):
            failures += 1
        if failures == 2:
            raise requests.RequestException("Both catalogs unavailable")
        return out
    events = fetch(GAMMA + "/public-search", params={"q": asset + " above", "events_status": "active",
                   "limit_per_type": 50}).get("events", [])
    out = {}
    for e in sorted(events, key=lambda e: "close-above" not in e["slug"]):
        if not e["slug"].startswith((asset.lower()+"-close-above-", asset.lower()+"-above-")):
            continue
        markets = [m for m in e["markets"] if m.get("acceptingOrders") and not m.get("closed")
                   and asset in m.get("question", "") and re.fullmatch(r"\$[\d,]+(?:\.\d+)?", m.get("groupItemTitle", ""))
                   and json.loads(m["outcomes"]) == ["Yes", "No"] and to_utc(m["endDate"]) > now
                   and (not m.get("feesEnabled") or m.get("feeSchedule", {}).get("exponent") == 1)]
        if markets:
            out.setdefault(expiry_key(markets[0]["endDate"]), (e["slug"], markets))
    return out


def books(asset, expiry, event):
    if asset in CRYPTO:
        result = []
        if event.get("gemini"):
            try:
                result = asyncio.run(asyncio.wait_for(gemini_snapshot(asset, event["gemini"]), timeout=8))
            except TimeoutError:
                if not event.get("kalshi"):
                    raise requests.Timeout("Gemini books timed out")
        markets = []
        if event.get("kalshi"):
            try:
                markets = fetch(BASE_URL + "/markets", params={"event_ticker": event["kalshi"], "limit": 1000, "status": "open"})["markets"]
            except requests.RequestException:
                if not result:
                    raise
        now = datetime.now(timezone.utc)
        for m in markets:
            if m.get("strike_type") != "greater" or expiry_key(m["close_time"]) != expiry:
                continue
            bids, asks = _kalshi_top(m)
            if bids or asks:
                strike = round(m["floor_strike"] + (0.0001 if asset == "SOL" else 0.01), 4)
                result.append(Book(venue="kalshi", asset=asset, contract_id=m["ticker"], strike=strike, lo=strike,
                    expiry=m["close_time"], updated=now, yes_bids=bids, yes_asks=asks,
                    settlement_index=f"CF Benchmarks {asset} 60-second average", link="https://kalshi.com/markets/"+CRYPTO[asset].lower()))
        return result
    slug, markets = event
    ids = [token for m in markets for token in json.loads(m["clobTokenIds"])]
    response = requests.post(CLOB+"/books", json=[{"token_id": t} for t in ids], timeout=12)
    response.raise_for_status()
    by_id = {b["asset_id"]: b for b in response.json() if "asset_id" in b}
    result = []
    for m in markets:
        yes_id, no_id = json.loads(m["clobTokenIds"])
        if yes_id not in by_id or no_id not in by_id or expiry_key(m["endDate"]) != expiry:
            continue
        yes, no = by_id[yes_id], by_id[no_id]
        strike = float(m["groupItemTitle"][1:].replace(",", ""))
        asks = [Level(price=float(x["price"]), size=float(x["size"])) for x in yes["asks"]]
        bids = [Level(price=round(1-float(x["price"]), 6), size=float(x["size"])) for x in no["asks"]]
        if bids or asks:
            result.append(Book(venue="polymarket", asset=asset, contract_id=m["conditionId"], strike=strike, lo=strike,
                expiry=m["endDate"], updated=datetime.fromtimestamp(min(int(yes["timestamp"]),int(no["timestamp"]))/1000, timezone.utc),
                yes_bids=bids, yes_asks=asks, fee_rate=m["feeSchedule"]["rate"] if m.get("feesEnabled") else 0,
                settlement_index=f"{asset} official close · {slug}", link="https://polymarket.com/event/"+slug))
    return result


class AssetMarkets:
    def __init__(self):
        self.cache = {}
        self.locks = {}
        self.histories = {}

    async def surface(self, asset):
        if asset == "ETH":
            try:
                return await self.cached(asset, (asset, "surface"), 90, eth_surface, asset)
            except HTTPException:
                return None

    async def get(self, asset, expiry=None):
        listings = await self.cached(asset, asset, 60, catalog, asset)
        now = datetime.now(timezone.utc)
        if expiry is None:
            return sorted(k for k in listings if to_utc(k) > now)
        try:
            key = expiry_key(expiry)
        except ValueError:
            raise HTTPException(422, "Invalid expiry")
        if key not in listings or to_utc(key) <= now:
            raise HTTPException(404, "This expiry has no open listed contracts")
        result = await self.cached(asset, (asset,key), 12, books, asset, key, listings[key])
        if not result:
            raise HTTPException(503, "This market currently has no visible depth")
        return result

    async def cached(self, asset, key, ttl, function, *args):
        async with self.locks.setdefault(asset, asyncio.Lock()):
            now = monotonic()
            self.cache = {k:v for k,v in self.cache.items() if v[0] > now}
            if key not in self.cache:
                try:
                    value = await asyncio.to_thread(function, *args)
                except (requests.RequestException, KeyError, TypeError, ValueError, IndexError) as exc:
                    raise HTTPException(503, f"{asset} prediction-market feed is unavailable") from exc
                if function is books:
                    self.histories = {k:v for k,v in self.histories.items() if to_utc(k[1]) > datetime.now(timezone.utc)}
                    history = self.histories.setdefault(key, History(max_age_s=600))
                    for book in value:
                        history.record_book(book, datetime.now(timezone.utc))
                self.cache[key] = (monotonic()+ttl, value)
            return self.cache[key][1]
