"""Print a few normalized Kalshi books from the live public API: full depth near spot, top of book elsewhere.

Usage:
    python scripts/debug_kalshi.py [--asset BTC] [--n 6] [--rps 12] [--window 0.03]
"""
from __future__ import annotations

import argparse
import asyncio
import time

from _debug import fmt_book, fmt_stats

from backend.ingest import deribit, kalshi


async def main(asset: str, n: int, rps: float, window_pct: float) -> None:
    client, dclient = kalshi.make_client(rps), deribit.make_client()
    try:
        spot = await deribit.fetch_spot(dclient, asset)
        window = (spot * (1 - window_pct), spot * (1 + window_pct))
        t0 = time.monotonic()
        markets = await kalshi.list_markets(client, asset)
        expiries = sorted({kalshi.expiry_of(m) for s, m in markets if s == kalshi.threshold_series(asset)})
        print(f"spot {spot:,.2f}; full-depth window {window[0]:,.0f} to {window[1]:,.0f}")
        print(f"{len(markets)} open markets; threshold expiries: {expiries}")
        if not expiries:
            return
        jobs = [(s, m) for s, m in markets if kalshi.expiry_of(m) == expiries[0]]
        near = [(s, m) for s, m in jobs if kalshi.in_window(m, window)]
        far = [(s, m) for s, m in jobs if not kalshi.in_window(m, window)]
        full = await asyncio.gather(*(kalshi.fetch_book(client, s, m, asset) for s, m in near),
                                    return_exceptions=True)
        errors = [r for r in full if isinstance(r, Exception)]
        full = [b for b in full if b is not None and not isinstance(b, Exception)]
        top = [b for b in (kalshi.top_book(s, m, asset) for s, m in far) if b is not None]
        print(f"expiry {expiries[0]}: {len(full)} full-depth books ({len(errors)} errors), "
              f"{len(top)} top-of-book, {time.monotonic() - t0:.1f}s")
        by_dist = lambda b: abs((b.strike if b.kind == "threshold" else (b.lo or b.hi)) - spot)
        for label, books in (("thresholds, full depth", [b for b in full if b.kind == "threshold"]),
                             ("buckets, full depth", [b for b in full if b.kind == "bucket"]),
                             ("top of book (outside window)", top)):
            print(f"-- {label}: {len(books)}")
            for b in sorted(books, key=by_dist)[:n]:
                print(" ", fmt_book(b))
        for e in errors[:3]:
            print("  ERROR", type(e).__name__, e)
        print(fmt_stats(client))
    finally:
        await client.aclose()
        await dclient.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--rps", type=float, default=12.0)
    ap.add_argument("--window", type=float, default=0.03)
    a = ap.parse_args()
    asyncio.run(main(a.asset, a.n, a.rps, a.window))
