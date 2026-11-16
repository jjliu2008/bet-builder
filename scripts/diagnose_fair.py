"""Diagnose fair value: compare the model-free Deribit call-spread digitals with our SVI digitals and the venue mids.

For each pair of adjacent listed call strikes K1 < K2 at a Deribit expiry:

    digital(Kmid) ~= (C(K1) - C(K2)) / (K2 - K1),    C = mark_price (BTC) * underlying_price ($)

The same call spread is computed from our SVI fit (black_call off the fitted smile), so the two
are compared like for like. For the venue expiry T, each Deribit expiry's digital is interpolated
to the venue strikes, then blended linearly in time between the bracketing expiries.

Usage:  python scripts/diagnose_fair.py [--expiry 2026-11-17T22:00] [--window 0.06]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.books import to_utc  # noqa: E402
from engine.dev_fetch import fetch_all, fetch_spot, list_expiries  # noqa: E402
from engine.fairvalue import (_parse_instrument, black_call, build_surface,  # noqa: E402
                              fair_above, fetch_deribit_rows)


def call_spreads(rows, slice_, lo, hi):
    """Return rows (Kmid, K1, K2, model-free digital, SVI digital) for adjacent call strikes in [lo, hi]."""
    calls = {}
    for r in rows:
        p = _parse_instrument(r.get("instrument_name", ""))
        if not p or p[0] != slice_.expiry or p[2] != "C" or not r.get("mark_price"):
            continue
        calls[p[1]] = r["mark_price"] * r["underlying_price"]
    ks = sorted(k for k in calls if lo <= k <= hi)
    out = []
    F = slice_.forward
    for k1, k2 in zip(ks, ks[1:]):
        mf = (calls[k1] - calls[k2]) / (k2 - k1)
        c = black_call(F, np.array([k1, k2]), slice_.total_variance(np.log(np.array([k1, k2]) / F)))
        out.append(((k1 + k2) / 2, k1, k2, mf, float((c[0] - c[1]) / (k2 - k1))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--expiry", default="2026-11-17T22:00")
    ap.add_argument("--window", type=float, default=0.06, help="strike window around spot, fraction")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    rows = fetch_deribit_rows(args.asset)
    surf = build_surface(rows, args.asset, now)
    spot = fetch_spot(args.asset)
    lo, hi = spot * (1 - args.window), spot * (1 + args.window)
    print(f"now {now:%Y-%m-%d %H:%M}Z  spot {spot:,.0f}  venue expiry {args.expiry}  "
          f"(matched: {list_expiries(args.asset)})")

    T = to_utc(args.expiry)
    s1, s2 = surf.slices[0], surf.slices[1]
    grids = {}
    for s in (s1, s2):
        cs = call_spreads(rows, s, lo, hi)
        grids[s.expiry] = cs
        print(f"\nDeribit {s.expiry:%Y-%m-%d %H:%M}Z  tau={s.tau * 365:.2f}d  F={s.forward:,.0f}  "
              f"OTM pts={len(s.k)}")
        print(f"{'K1':>7} {'K2':>7} {'call-spread':>12} {'SVI spread':>11} {'SVI-MF':>8}")
        for kmid, k1, k2, mf, sv in cs:
            print(f"{k1:>7.0f} {k2:>7.0f} {mf:>12.3f} {sv:>11.3f} {sv - mf:>+8.3f}")
        if cs:
            d = np.array([sv - mf for *_, mf, sv in cs])
            print(f"mean SVI-MF {d.mean():+.3f}  max |SVI-MF| {np.abs(d).max():.3f}")

    # blend to the venue expiry and compare with the venue mids
    tT = surf.tau(T)
    x = min(max((tT - s1.tau) / (s2.tau - s1.tau), 0.0), 1.0)
    books = fetch_all(args.asset, args.expiry)
    by_k = {}
    for b in books:
        if b.kind == "threshold":
            by_k.setdefault(b.strike, {})[b.venue] = b
    strikes = sorted(k for k in by_k if lo <= k <= hi)
    fair = fair_above(args.asset, T, strikes, surface=surf)

    def interp(grid, k, col):
        g = sorted(grid)
        return float(np.interp(k, [r[0] for r in g], [r[col] for r in g])) if g else float("nan")

    print(f"\nVenue expiry {args.expiry}: blend weight on {s2.expiry:%b %d} = {x:.2f}")
    print(f"{'strike':>7} {'MF e1':>6} {'SVI e1':>6} {'MF e2':>6} {'SVI e2':>6} {'MF@T':>6} {'SVI@T':>6} "
          f"{'fair':>6} {'gem':>6} {'kal':>6} {'venue':>6} {'MF-ven':>7} {'fair-MF':>7}")
    stats = []
    for k in strikes:
        m1, v1 = interp(grids[s1.expiry], k, 3), interp(grids[s1.expiry], k, 4)
        m2, v2 = interp(grids[s2.expiry], k, 3), interp(grids[s2.expiry], k, 4)
        mT, vT = m1 + x * (m2 - m1), v1 + x * (v2 - v1)
        g, kk = by_k[k].get("gemini"), by_k[k].get("kalshi")
        mids = [b.mid for b in (g, kk) if b is not None and b.mid is not None]
        ven = sum(mids) / len(mids) if mids else None
        fm = lambda b: f"{b.mid:.3f}" if b is not None and b.mid is not None else "  -  "
        print(f"{k:>7.0f} {m1:>6.3f} {v1:>6.3f} {m2:>6.3f} {v2:>6.3f} {mT:>6.3f} {vT:>6.3f} "
              f"{fair[k]:>6.3f} {fm(g):>6} {fm(kk):>6} "
              f"{(f'{ven:.3f}' if ven is not None else '  -  '):>6} "
              f"{(f'{mT - ven:+.3f}' if ven is not None else '   -  '):>7} {fair[k] - mT:>+7.3f}")
        if ven is not None and 0.05 < ven < 0.95:
            stats.append((mT - ven, fair[k] - mT, fair[k] - ven))
    if stats:
        a = np.array(stats)
        print(f"\nliquid strikes: {len(a)}")
        print(f"  mean (model-free - venue mid) = {a[:, 0].mean():+.3f}   mean |.| = {np.abs(a[:, 0]).mean():.3f}")
        print(f"  mean (our fair - model-free)  = {a[:, 1].mean():+.3f}   mean |.| = {np.abs(a[:, 1]).mean():.3f}")
        print(f"  mean (our fair - venue mid)   = {a[:, 2].mean():+.3f}")


if __name__ == "__main__":
    main()
