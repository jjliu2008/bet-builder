"""Print the contract matcher's report for the live venue listings.

Usage:
    python scripts/debug_matcher.py [--asset BTC] [--full]

Only the two listings are fetched (three requests), not the order books. Strikes are printed in
full. Without --full, long lists of pairs and reasons are cut to a few lines, with the count shown.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

import _debug  # noqa: F401  (sets sys.path)

from backend.ingest import gemini, kalshi
from backend.ingest.poller import catalog_from_listings
from backend.matcher import build_report, fmt_strike


def strikes(xs) -> str:
    return " ".join(fmt_strike(x) for x in xs) or "(none)"


def show(report, full: bool) -> None:
    cap = None if full else 4
    s = report.summary()
    print(f"asset {report.asset}, built {report.built_at}; input contracts: {report.contracts}")
    print(f"summary: {s}")
    for w in report.warnings:
        print(f"WARNING: {w}")
    print(f"\nmatched expiries (unexpired, on both venues): {report.matched}")
    for key, m in report.expiries.items():
        gem_only, kal_only = m.unmatched_on("gemini"), m.unmatched_on("kalshi")
        differs = sum(p.index_differs for p in m.pairs)
        print(f"\n=== {key}{'  [EXPIRED]' if m.expired else ''}")
        print(f"  pairs: {len(m.pairs)}   settlement index differs on {differs} of {len(m.pairs)}")
        print(f"    strikes: {strikes(p.strike for p in m.pairs)}")
        for p in m.pairs[:cap]:
            print(f"    {fmt_strike(p.strike):>7}  {p.gemini:30s} <-> {p.kalshi:30s} index_differs={p.index_differs}")
        if cap and len(m.pairs) > cap:
            print(f"    ... {len(m.pairs) - cap} more pairs (use --full)")
        if m.pairs:
            p = m.pairs[0]
            print(f"    gemini settles on: {p.gemini_index}\n    kalshi settles on: {p.kalshi_index}")
        if m.buckets:
            los = [b.lo for b in m.buckets if b.lo is not None]
            his = [b.hi for b in m.buckets if b.hi is not None]
            tails = sum(b.lo is None or b.hi is None for b in m.buckets)
            print(f"  kalshi buckets: {len(m.buckets)}, covering {fmt_strike(min(los))} to {fmt_strike(max(his))}"
                  f" ({tails} open-ended)   e.g. {m.buckets[len(m.buckets) // 2].contract_id}")
        else:
            print("  kalshi buckets: 0")
        for venue, items in (("gemini", gem_only), ("kalshi", kal_only)):
            print(f"  unmatched on {venue}: {len(items)}")
            if items:
                print(f"    strikes: {strikes(u.strike for u in items)}")
                for u in items[:cap]:
                    print(f"    - {u.reason}  [{u.contract_id}]")
                if cap and len(items) > cap:
                    print(f"    ... {len(items) - cap} more with the same form of reason (use --full)")
    print(f"\nunmatched expiries: {len(report.unmatched_expiries)}")
    for u in report.unmatched_expiries:
        print(f"  - {u.reason}{'  [EXPIRED]' if u.expired else ''}")
        print(f"    {u.venue} strikes: {strikes(u.strikes)}  ({u.contracts} contracts)")
    print(f"\nissues: {len(report.issues)}")
    for i in report.issues:
        print(f"  - {i.venue} {i.contract_id}: {i.reason}")


async def main(asset: str, full: bool) -> None:
    g, k = gemini.make_client(), kalshi.make_client()
    try:
        gem_list, kal_list = await asyncio.gather(gemini.list_contracts(g, asset), kalshi.list_markets(k, asset))
    finally:
        await g.aclose()
        await k.aclose()
    now = datetime.now(timezone.utc)
    gem, kal, gem_bad, kal_bad = catalog_from_listings(gem_list, kal_list, asset, now)
    show(build_report(gem + kal, now, asset, gem_bad + kal_bad), full)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--full", action="store_true", help="print every pair and every reason")
    a = ap.parse_args()
    asyncio.run(main(a.asset, a.full))
