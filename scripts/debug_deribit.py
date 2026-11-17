"""Print the live Deribit spot, the fitted surface's expiries, and a few fair values.

Usage:
    python scripts/debug_deribit.py [--asset BTC]
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from _debug import fmt_stats

from backend.ingest import deribit
from engine import build_surface, expiry_key, fair_above


async def main(asset: str) -> None:
    client = deribit.make_client()
    try:
        spot, rows = await asyncio.gather(deribit.fetch_spot(client, asset), deribit.fetch_rows(client, asset))
        print(f"{asset} spot (Deribit index) = {spot:,.2f}")
        print(f"{len(rows)} option summaries; sample: "
              f"{ {k: rows[0][k] for k in ('instrument_name', 'mark_iv', 'underlying_price')} }")
        now = datetime.now(timezone.utc)
        surface = build_surface(rows, asset, now)
        print(f"surface: {len(surface.slices)} expiries")
        for s in surface.slices[:6]:
            fit = "SVI" if s.params is not None else "interpolated"
            print(f"  {s.expiry:%Y-%m-%d %H:%M}Z  tau={s.tau * 365:6.2f}d  F={s.forward:,.0f}  points={len(s.k):3d}  {fit}")
        step = 500
        base = round(spot / step) * step
        strikes = [base + i * step for i in range(-3, 4)]
        for hours in (24, 72):
            expiry = expiry_key(now + timedelta(hours=hours))
            fair = fair_above(asset, expiry, strikes, surface=surface)
            print(f"P(S >= K) at {expiry} (+{hours}h): " + "  ".join(f"{k:g}:{p:.3f}" for k, p in fair.items()))
        print(fmt_stats(client))
    finally:
        await client.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    asyncio.run(main(ap.parse_args().asset))
