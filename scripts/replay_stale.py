"""Replay a saved snapshot log (JSON lines) through the stale detector and print every flag with its reason.

Usage:
    python scripts/replay_stale.py docs/samples/snapshots_sample.jsonl
    python scripts/replay_stale.py LOG.jsonl --every            # also print flags that stay on
    python scripts/replay_stale.py LOG.jsonl --min-gap 0.03 --window 180 --move-bp 20 --fair-band 0.02
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.stale import StaleConfig, load_snapshots, replay  # noqa: E402


def main():
    d = StaleConfig()
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--every", action="store_true", help="print active flags at every snapshot, not just transitions")
    ap.add_argument("--min-gap", type=float, default=d.min_gap, help="cross-venue mid gap, $")
    ap.add_argument("--window", type=float, default=d.frozen_window_s, help="frozen window, seconds")
    ap.add_argument("--move-bp", type=float, default=d.btc_move_bp, help="BTC move over window, bp")
    ap.add_argument("--fair-band", type=float, default=d.fair_band, help="'agrees with fair' band, $")
    args = ap.parse_args()

    cfg = StaleConfig(min_gap=args.min_gap, frozen_window_s=args.window, btc_move_bp=args.move_bp,
                      fair_band=args.fair_band)
    records = load_snapshots(args.path)
    n_books = sum(r.get("type", "book") == "book" for r in records)
    print(f"{args.path}: {len(records)} records ({n_books} book snapshots)")
    events = replay(records, cfg, every=args.every)
    for e in events:
        print(f"{e.ts:%Y-%m-%d %H:%M:%S}Z  {e.event.upper():8s} {e.asset} {e.expiry} "
              f"{e.strike:>8.0f} {e.venue:7s} {e.reason}")
    raised = sum(e.event == "raised" for e in events)
    print(f"{raised} flag(s) raised, {sum(e.event == 'cleared' for e in events)} cleared")


if __name__ == "__main__":
    main()
