"""Run the production poller for a few cycles and print per-poll request and 429 counts.

Usage:
    python scripts/debug_poll.py [--polls 3] [--interval 30]

Rates and the window come from the same environment variables as the server (KALSHI_RPS, ...).
"""
from __future__ import annotations

import argparse
import asyncio
import time

import _debug  # noqa: F401  (sets sys.path)

from backend.ingest.poller import SOURCES, Poller, Settings


async def main(polls: int, interval: float) -> None:
    settings = Settings.from_env()
    settings.poll_interval_s = interval
    poller = Poller(settings=settings)
    st = poller.state
    print(f"settings: {settings}")
    prev = {s: (0, 0) for s in SOURCES}
    total_429 = 0
    try:
        for i in range(polls):
            t0 = time.monotonic()
            await poller.poll_once()
            took = time.monotonic() - t0
            print(f"\npoll {i + 1}/{polls}: {took:.1f}s  status={st.status()}  spot={st.spot:,.2f}  "
                  f"expiries={st.expiries}")
            for s in SOURCES:
                c, src = st.stats[s], st.sources[s]
                dr, d429 = c.requests - prev[s][0], c.http_429 - prev[s][1]
                prev[s] = (c.requests, c.http_429)
                total_429 += d429
                books = f"books={len(st.books[s]):4d}" if s in st.books else "          "
                print(f"  {s:8s} ok={src.ok!s:5s} {books} requests={dr:4d} http_429={d429:3d} "
                      f"last_error={src.last_error}")
            if i < polls - 1:
                await asyncio.sleep(max(0.0, interval - took))
        print(f"\ntotal http_429 over {polls} polls: {total_429}")
    finally:
        await poller.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--polls", type=int, default=3)
    ap.add_argument("--interval", type=float, default=30.0)
    a = ap.parse_args()
    asyncio.run(main(a.polls, a.interval))
