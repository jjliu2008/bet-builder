"""Live demo: fetch public Gemini + Kalshi books and print the cheapest routes for a few BTC bets.

Usage:  python scripts/live_demo.py [--expiry 2026-11-20T22:00] [--size 10]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import RouteRequest, StrikeNotFound, RouteError, price_route  # noqa: E402
from engine.dev_fetch import fetch_all, fetch_spot, list_expiries, matched_expiries  # noqa: E402
from engine.fairvalue import fair_above, get_surface, make_fair_fn  # noqa: E402


def nearest_strike(strikes, x):
    return min(strikes, key=lambda k: abs(k - x))


def show_route(label, req, books, **kw):
    print(f"\n== {label}  ({req.type} lo={req.lo} hi={req.hi} size={req.size:g})")
    try:
        r = price_route(req, books, **kw)
    except (StrikeNotFound, RouteError) as e:
        print("   ERROR:", e)
        return None
    for l in r.legs:
        print(f"   {l.venue:7s} {l.side:3s} {l.contract:34s} x{l.size:<7g} @ {l.price:.4f}  fee/ct {l.fee:.4f}  depth {l.size_available:g}")
    print(f"   build={r.build}  cash/ct={r.cash_outlay:.4f}  effective/ct={r.total_cost:.4f}  "
          f"best single venue={r.best_single_venue_cost}  savings={r.savings}  mixed_index={r.mixed_settlement_index}")
    if r.fair_value is not None:
        print(f"   fair={r.fair_value:.4f}  verdict={r.verdict}")
    for w in r.warnings + r.stale_warnings:
        print("   !", w)
    alts = ", ".join(f"{a.build}[{'+'.join(a.venues)}]={a.total_cost:.4f}" for a in r.alternatives)
    print(f"   alternatives: {alts}")
    return r


def fair_table(books, expiry, surface, spot):
    """Print venue mids vs Deribit fair for every strike listed on both venues; return {K: fair}."""
    by_k = {}
    for b in books:
        if b.kind == "threshold":
            by_k.setdefault(b.strike, {})[b.venue] = b
    fair = fair_above("BTC", expiry, list(by_k), surface=surface)
    print()
    print(f"{'strike':>8} {'gem bid/ask':>13} {'kal bid/ask':>13} {'gem mid':>8} {'kal mid':>8} {'fair':>7}  check")
    mono_ok, inside, liquid = True, 0, 0
    prev = 1.0
    for k in sorted(by_k):
        g, kk = by_k[k].get("gemini"), by_k[k].get("kalshi")
        if g is None or kk is None:
            continue
        f = fair[k]
        mono_ok &= f <= prev + 1e-12
        prev = f
        mids = [m for m in (g.mid, kk.mid) if m is not None]
        tag = ""
        if len(mids) == 2 and 0.05 < min(mids) and max(mids) < 0.95:
            liquid += 1
            lo, hi = min(mids) - 0.02, max(mids) + 0.02
            ok = lo <= f <= hi
            inside += ok
            tag = "ok" if ok else "OUTSIDE mids+/-2c"
        q = lambda b: f"{b.best_bid or 0:.2f}/{b.best_ask or 0:.2f}"
        m = lambda b: f"{b.mid:.3f}" if b.mid is not None else "  -  "
        print(f"{k:>8.0f} {q(g):>13} {q(kk):>13} {m(g):>8} {m(kk):>8} {f:>7.3f}  {tag}")
    print(f"fair monotone decreasing in K: {mono_ok};  liquid strikes with fair inside venue mids (+/-2c): {inside}/{liquid}")
    return fair


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--expiry", default=None, help="expiry key, default: nearest matched expiry")
    ap.add_argument("--size", type=float, default=10)
    args = ap.parse_args()

    spot = fetch_spot(args.asset)
    expiries = list_expiries(args.asset)
    print(f"{args.asset} spot (Deribit index) = {spot:,.2f}")
    print("matched expiries:", expiries)
    if not expiries:
        print("no matched expiries right now")
        return
    expiry = args.expiry or expiries[0]
    books = fetch_all(args.asset, expiry)
    print(f"expiry {expiry}: {sum(b.venue == 'gemini' for b in books)} Gemini books, "
          f"{sum(b.venue == 'kalshi' for b in books)} Kalshi books; matched={matched_expiries(books, args.asset)}")

    surface = get_surface(args.asset)
    print(f"Deribit surface: {len(surface.slices)} expiries, first {surface.slices[0].expiry:%Y-%m-%d %H:%M}Z")
    fair = fair_table(books, expiry, surface, spot)
    fair_fn = make_fair_fn(surface)

    gem_strikes = sorted({b.strike for b in books if b.venue == "gemini" and b.kind == "threshold"})
    if not gem_strikes:
        print("no Gemini strikes at this expiry")
        return
    k_atm = nearest_strike(gem_strikes, spot)
    k_up = nearest_strike(gem_strikes, spot * 1.02)
    k_dn = nearest_strike(gem_strikes, spot * 0.98)

    mk = lambda **kw: RouteRequest(asset=args.asset, expiry=expiry, size=args.size, **kw)
    kw = dict(fair_fn=fair_fn)
    show_route("above ATM", mk(type="above", lo=k_atm), books, **kw)
    show_route("above +2%", mk(type="above", lo=k_up), books, **kw)
    show_route("below -2%", mk(type="below", hi=k_dn), books, **kw)
    show_route("between -2%..+2%", mk(type="between", lo=k_dn, hi=k_up), books, **kw)
    show_route("between ATM..ATM+1000", mk(type="between", lo=k_atm, hi=k_atm + 1000), books, **kw)
    show_route("bad strike", mk(type="above", lo=k_atm + 123), books, **kw)


if __name__ == "__main__":
    main()
