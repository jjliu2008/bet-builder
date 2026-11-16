"""Thin public-API fetchers for testing and the live demo.

The production ingest and contract matcher belong to Person 2 (backend/). These fetchers
exist so the engine can be exercised end-to-end against live data. Every endpoint is
public, and no keys are used.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from .books import Book, Level, expiry_key, to_utc

GEMINI = "https://api.gemini.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
DERIBIT = "https://www.deribit.com/api/v2/public"
UA = {"User-Agent": "bet-builder-dev/0.1"}
TIMEOUT = 15

GEMINI_INDEX = "Gemini BTC index (LS-LRRR-XBTUSD)"
KALSHI_INDEX = "CF Benchmarks BRTI, 60s average"


def _get(url: str, params: Optional[dict] = None, session: Optional[requests.Session] = None,
         retries: int = 4) -> dict:
    s = session or requests
    for attempt in range(retries):
        r = s.get(url, params=params, headers=UA, timeout=TIMEOUT)
        if r.status_code == 429 and attempt < retries - 1:
            time.sleep(0.5 * 2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def _f(x) -> float:
    return float(x) if x not in (None, "") else 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- Gemini ------------------------------------------------------------------------------
def gemini_events(asset: str = "BTC", session=None) -> List[dict]:
    """Return the active Gemini Crypto events for `asset` that list 'above' strike ladders.

    Short-interval Up/Down events (BTC05M, BTC15M) are excluded. Pagination follows
    `pagination.total`.
    """
    out, offset = [], 0
    pat = re.compile(rf"^{asset.upper()}\d{{10}}$")      # e.g. BTC2611202200
    while True:
        d = _get(f"{GEMINI}/v1/prediction-markets/events",
                 {"status": "active", "category": "Crypto", "limit": 100, "offset": offset}, session)
        data = d.get("data", [])
        out += [e for e in data if pat.match(e.get("ticker", ""))
                and any((c.get("strike") or {}).get("type") == "above" for c in e.get("contracts", []))]
        offset += len(data)
        if not data or offset >= int(d.get("pagination", {}).get("total", 0)):
            return out


def parse_gemini_book(contract: dict, event: dict, book_json: dict, asset: str = "BTC",
                      fetched_at: Optional[datetime] = None) -> Book:
    """Normalize one Gemini contract and its /v1/book response into a Book.

    Prices are in $/contract and amounts are in contracts. `updated` is the newest level
    timestamp, or `fetched_at` if there is none.
    """
    bids = [Level(price=_f(l["price"]), size=_f(l["amount"])) for l in book_json.get("bids", []) if _f(l["amount"]) > 0]
    asks = [Level(price=_f(l["price"]), size=_f(l["amount"])) for l in book_json.get("asks", []) if _f(l["amount"]) > 0]
    ts = [int(l["timestamp"]) for l in book_json.get("bids", []) + book_json.get("asks", []) if l.get("timestamp")]
    updated = datetime.fromtimestamp(max(ts), timezone.utc) if ts else (fetched_at or _now())
    k = _f(contract["strike"]["value"])
    return Book(venue="gemini", contract_id=contract["instrumentSymbol"], strike=k, lo=k,
                expiry=contract.get("expiryDate") or event["expiryDate"], kind="threshold",
                yes_bids=bids, yes_asks=asks, updated=updated, asset=asset.upper(),
                settlement_index=GEMINI_INDEX,
                link=f"https://www.gemini.com/predictions/{event.get('slug', '')}")


def fetch_gemini_books(asset: str = "BTC", expiry: Optional[str] = None, workers: int = 8,
                       session=None) -> List[Book]:
    """Fetch the live Gemini threshold books for `asset`, optionally for a single expiry.

    Args:
        asset: e.g. "BTC".
        expiry: an expiry key such as '2026-11-20T22:00'. None fetches every expiry.
        workers: the number of parallel book requests.

    Returns:
        A list of Books with kind "threshold" (YES pays if S_T >= strike).
    """
    jobs = []
    for e in gemini_events(asset, session):
        for c in e.get("contracts", []):
            if (c.get("strike") or {}).get("type") != "above":
                continue
            if expiry and expiry_key(c.get("expiryDate") or e["expiryDate"]) != expiry:
                continue
            jobs.append((c, e))

    def one(job):
        c, e = job
        bj = _get(f"{GEMINI}/v1/book/{c['instrumentSymbol']}", {"limit_bids": 10, "limit_asks": 10}, session)
        return parse_gemini_book(c, e, bj, asset, _now())

    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, jobs))


# ---- Kalshi ------------------------------------------------------------------------------
def _edge(x: Optional[float]) -> Optional[float]:
    """Map a Kalshi strike like 67999.99 to the $ boundary it stands for (68000)."""
    if x is None:
        return None
    cents = round(x * 100)
    return round((cents + 1) / 100, 2) if cents % 100 == 99 else round(x, 2)


def _levels(raw) -> List[Tuple[float, float]]:
    """Parse Kalshi [price, size] pairs. Prices may be in dollars or cents, and either can be strings."""
    out = []
    for p, s in raw or []:
        p, s = _f(p), _f(s)
        if p > 1.0:
            p /= 100.0
        if s > 0:
            out.append((round(p, 4), s))
    return out


def parse_kalshi_orderbook(d: dict) -> Tuple[List[Level], List[Level]]:
    """Convert a Kalshi /orderbook response into YES (bids, asks) in $/contract.

    Kalshi lists YES bids and NO bids. A NO bid at q is a YES ask at 1 - q. Both
    `orderbook_fp` with `yes_dollars`/`no_dollars` and the older `orderbook` with `yes`/`no`
    (cents) are accepted.
    """
    ob = d.get("orderbook_fp") or d.get("orderbook") or {}
    yes = _levels(ob.get("yes_dollars") or ob.get("yes"))
    no = _levels(ob.get("no_dollars") or ob.get("no"))
    bids = [Level(price=p, size=s) for p, s in yes]
    asks = [Level(price=round(1 - q, 4), size=s) for q, s in no]
    return bids, asks


def _kalshi_top(m: dict) -> Tuple[List[Level], List[Level]]:
    """Return top-of-book from a nested market. Used when the orderbook request fails or depth='top'."""
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    bs, as_ = _f(m.get("yes_bid_size_fp")), _f(m.get("yes_ask_size_fp"))
    bids = [Level(price=bid, size=bs)] if bid > 0 and bs > 0 else []
    asks = [Level(price=ask, size=as_)] if 0 < ask < 1 and as_ > 0 else []
    return bids, asks


def kalshi_market_to_book(m: dict, series: str, bids: List[Level], asks: List[Level],
                          asset: str = "BTC", fetched_at: Optional[datetime] = None) -> Optional[Book]:
    """Normalize a Kalshi market plus its YES levels into a Book.

    Strike types map as follows:
    - 'greater' on KX<asset>D: a threshold at floor + 0.01.
    - 'between': a bucket [floor, cap + 0.01).
    - 'less': a lower-tail bucket (-inf, cap).
    - 'greater' on the bucket series: an upper-tail bucket [floor + 0.01, inf).

    Returns None for strike types it doesn't recognize.
    """
    st = m.get("strike_type")
    lo, hi = _edge(m.get("floor_strike")), _edge(m.get("cap_strike"))
    common = dict(venue="kalshi", contract_id=m["ticker"], expiry=m["close_time"], yes_bids=bids,
                  yes_asks=asks, updated=fetched_at or _now(), asset=asset.upper(),
                  settlement_index=KALSHI_INDEX,
                  link=f"https://kalshi.com/markets/{series.lower()}")
    if st == "greater" and series.endswith("D"):
        return Book(kind="threshold", strike=lo, lo=lo, **common)
    if st == "greater":
        return Book(kind="bucket", lo=lo, hi=None, **common)
    if st == "between":
        return Book(kind="bucket", lo=lo, hi=hi, **common)
    if st == "less":
        return Book(kind="bucket", lo=None, hi=hi, **common)
    return None


def fetch_kalshi_books(asset: str = "BTC", expiry: Optional[str] = None, depth: str = "full",
                       strike_range: Optional[Tuple[float, float]] = None, workers: int = 6,
                       session=None) -> List[Book]:
    """Fetch the live Kalshi threshold books (KX<asset>D) and range-bucket books (KX<asset>).

    Args:
        asset: e.g. "BTC".
        expiry: an expiry key. None fetches every open expiry.
        depth: "full" calls /orderbook?depth=10 per market. "top" uses only the event listing's
            best bid/ask and makes no extra requests.
        strike_range: an optional ($lo, $hi) window. Full books are fetched only for markets
            touching it; markets outside the window get top-of-book.
        workers: the number of parallel orderbook requests.

    Returns:
        A list of Books, both thresholds and buckets.
    """
    markets = []
    for series in (f"KX{asset.upper()}D", f"KX{asset.upper()}"):
        d = _get(f"{KALSHI}/events", {"series_ticker": series, "status": "open",
                                      "with_nested_markets": "true", "limit": 200}, session)
        for e in d.get("events", []):
            for m in e.get("markets", []):
                if expiry and expiry_key(m["close_time"]) != expiry:
                    continue
                markets.append((series, m))

    def in_window(m):
        if strike_range is None:
            return True
        lo = m.get("floor_strike") if m.get("floor_strike") is not None else -1e18
        hi = m.get("cap_strike") if m.get("cap_strike") is not None else 1e18
        return hi >= strike_range[0] and lo <= strike_range[1]

    def one(job):
        series, m = job
        bids, asks = _kalshi_top(m)
        if depth == "full" and in_window(m):
            try:
                bids, asks = parse_kalshi_orderbook(
                    _get(f"{KALSHI}/markets/{m['ticker']}/orderbook", {"depth": 10}, session))
            except requests.RequestException:
                pass
        return kalshi_market_to_book(m, series, bids, asks, asset, _now())

    with ThreadPoolExecutor(workers) as ex:
        return [b for b in ex.map(one, markets) if b is not None]


# ---- matching + spot ---------------------------------------------------------------------
def matched_expiries(books: Iterable[Book], asset: str = "BTC") -> List[str]:
    """Return the expiry keys (minute precision, UTC) that have threshold books on both Gemini and Kalshi."""
    by_venue: Dict[str, set] = {}
    for b in books:
        if b.asset == asset.upper() and b.kind == "threshold":
            by_venue.setdefault(b.venue, set()).add(expiry_key(b.expiry))
    return sorted(by_venue.get("gemini", set()) & by_venue.get("kalshi", set()))


def list_expiries(asset: str = "BTC", session=None) -> List[str]:
    """Return the matched expiries from the event listings alone, without fetching order books."""
    gem = {expiry_key(c.get("expiryDate") or e["expiryDate"])
           for e in gemini_events(asset, session) for c in e.get("contracts", [])}
    d = _get(f"{KALSHI}/events", {"series_ticker": f"KX{asset.upper()}D", "status": "open",
                                  "with_nested_markets": "true", "limit": 200}, session)
    kal = {expiry_key(m["close_time"]) for e in d.get("events", []) for m in e.get("markets", [])}
    return sorted(gem & kal)


def fetch_spot(asset: str = "BTC", session=None) -> float:
    """Return the Deribit index price for `asset`, in $."""
    d = _get(f"{DERIBIT}/get_index_price", {"index_name": f"{asset.lower()}_usd"}, session)
    return float(d["result"]["index_price"])


def fetch_all(asset: str = "BTC", expiry: Optional[str] = None, window_pct: float = 0.12,
              session=None) -> List[Book]:
    """Fetch Gemini and Kalshi books for one expiry. Kalshi depth is limited to strikes within window_pct of spot."""
    spot = fetch_spot(asset, session)
    win = (spot * (1 - window_pct), spot * (1 + window_pct))
    return (fetch_gemini_books(asset, expiry, session=session)
            + fetch_kalshi_books(asset, expiry, strike_range=win, session=session))
