"""Record one live poll's venue responses into backend/tests/fixtures/recorded.json for offline tests.

Usage:
    python scripts/record_ingest_fixtures.py

The responses are real, then trimmed so the file stays small:
- Gemini: BTC strike-ladder events plus two other events (to test the filter); only the fields we parse.
- Kalshi: markets for the polled expiries plus one more, within ±4.5% of spot; only the fields we parse.
- Deribit: three fields per option, for the five nearest expiries.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import httpx

import _debug  # noqa: F401  (sets sys.path)

from backend.ingest import gemini, kalshi
from backend.ingest.poller import Poller, Settings
from backend.tests.replay import FIXTURE, key_for
from engine.books import iso
from engine.fairvalue import _parse_instrument

LISTING_BAND = 0.045
MARKET_FIELDS = ("ticker", "close_time", "strike_type", "floor_strike", "cap_strike", "yes_bid_dollars",
                 "yes_ask_dollars", "yes_bid_size_fp", "yes_ask_size_fp")


def record(client, responses: dict) -> None:
    """Wrap `client.get` so every decoded JSON response is stored under its request key."""
    inner, base = client.get, str(client._client.base_url).rstrip("/")

    async def get(path, params=None):
        out = await inner(path, params)
        responses[key_for(httpx.URL(base + path, params=params))] = out
        return out

    client.get = get


def trim(responses: dict, spot: float, targets: list, asset: str = "BTC") -> dict:
    out = {}
    lo, hi = spot * (1 - LISTING_BAND), spot * (1 + LISTING_BAND)

    # Gemini events: merge the pages into one, keep ladder events and two others.
    pat = re.compile(rf"^{asset}\d{{10}}$")
    ladder, other, first_key = [], [], None
    for k in sorted(responses):
        if "/v1/prediction-markets/events" in k:
            first_key = first_key or re.sub(r"offset=\d+", "offset=0", k)
            for e in responses[k].get("data", []):
                ev = {"ticker": e.get("ticker"), "expiryDate": e.get("expiryDate"), "slug": e.get("slug"),
                      "contracts": [{f: c.get(f) for f in ("instrumentSymbol", "expiryDate", "strike")}
                                    for c in e.get("contracts", [])]}
                (ladder if pat.match(e.get("ticker", "")) else other).append(ev)
    data = ladder + other[:2]
    out[first_key] = {"data": data, "pagination": {"total": len(data)}}

    # Kalshi events: one page per series, markets near spot for the targets plus one more expiry.
    kept = set()
    for k in sorted(responses):
        if "/trade-api/v2/events" not in k or "cursor=" in k:
            continue
        pages = [responses[k]] + [v for kk, v in sorted(responses.items())
                                  if "/trade-api/v2/events" in kk and "cursor=" in kk
                                  and kk.split("series_ticker=")[1].split("&")[0] == k.split("series_ticker=")[1].split("&")[0]]
        all_exp = sorted({kalshi.expiry_of(m) for p in pages for e in p.get("events", []) for m in e.get("markets", [])})
        keep_exp = set(targets) | set([e for e in all_exp if e not in targets and e > min(targets)][:1])
        events = []
        for p in pages:
            for e in p.get("events", []):
                ms = []
                for m in e.get("markets", []):
                    if kalshi.expiry_of(m) in keep_exp and kalshi.in_window(m, (lo, hi)):
                        ms.append({f_: m.get(f_) for f_ in MARKET_FIELDS})
                        kept.add(m["ticker"])
                if ms:
                    events.append({"event_ticker": e.get("event_ticker"), "markets": ms})
        out[k] = {"events": events, "cursor": ""}

    for k, v in responses.items():
        if "/v1/book/" in k:
            out[k] = {side: [{f: l.get(f) for f in ("price", "amount", "timestamp")} for l in v.get(side, [])]
                      for side in ("bids", "asks")}
        elif "/orderbook" in k:
            if k.split("/markets/")[1].split("/orderbook")[0] in kept:
                out[k] = v
        elif "get_index_price" in k:
            out[k] = {"result": {"index_price": v["result"]["index_price"]}}
        elif "get_book_summary_by_currency" in k:
            rows = [(_parse_instrument(r["instrument_name"]), r) for r in v["result"]]
            exps = sorted({p[0] for p, _ in rows if p})[:5]
            out[k] = {"result": [{f: r.get(f) for f in ("instrument_name", "mark_iv", "underlying_price")}
                                 for p, r in rows if p and p[0] in exps]}
    return out


async def main() -> None:
    raw: dict = {}
    poller = Poller(settings=Settings.from_env())
    for c in poller.clients.values():
        record(c, raw)
    try:
        await poller.poll_once()
    finally:
        await poller.aclose()
    st = poller.state
    print(f"recorded {len(raw)} responses; status={st.status()} spot={st.spot} expiries={st.expiries}")
    for s, src in st.sources.items():
        print(f"  {s}: ok={src.ok} last_error={src.last_error}")
    responses = trim(raw, st.spot, st.expiries, st.asset)
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"meta": {"recorded_at": iso(st.last_poll), "spot": st.spot, "targets": st.expiries},
                   "responses": responses}, f, separators=(",", ":"), sort_keys=True)
    print(f"wrote {FIXTURE}: {len(responses)} responses, {os.path.getsize(FIXTURE) / 1024:.0f} KB")


if __name__ == "__main__":
    asyncio.run(main())
