"""Print a few normalized Gemini books from the live public API.

Usage:
    python scripts/debug_gemini.py [--asset BTC] [--n 8] [--rps 5]
"""
from __future__ import annotations

import argparse
import asyncio
import time

from _debug import fmt_book, fmt_stats

from backend.ingest import gemini


async def main(asset: str, n: int, rps: float) -> None:
    client = gemini.make_client(rps)
    try:
        t0 = time.monotonic()
        contracts = await gemini.list_contracts(client, asset)
        expiries = sorted({gemini.expiry_of(c, e) for c, e in contracts})
        print(f"{len(contracts)} 'above' contracts across {len(expiries)} expiries: {expiries}")
        if not contracts:
            return
        jobs = [(c, e) for c, e in contracts if gemini.expiry_of(c, e) == expiries[0]]
        results = await gemini.fetch_books(client, jobs, asset)
        books = sorted((b for b in results if not isinstance(b, Exception)), key=lambda b: b.strike)
        errors = [r for r in results if isinstance(r, Exception)]
        print(f"expiry {expiries[0]}: {len(books)} books, {len(errors)} errors, {time.monotonic() - t0:.1f}s")
        two_sided = [b for b in books if b.mid is not None]
        for b in (two_sided or books)[:n]:
            print(" ", fmt_book(b))
        for e in errors[:3]:
            print("  ERROR", type(e).__name__, e)
        print(fmt_stats(client))
    finally:
        await client.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--rps", type=float, default=5.0)
    a = ap.parse_args()
    asyncio.run(main(a.asset, a.n, a.rps))
