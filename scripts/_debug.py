"""Shared helpers for the scripts/debug_*.py ingest scripts."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def fmt_book(b) -> str:
    """One line per normalized book: bounds, best bid/ask in $/contract, top sizes, level counts."""
    px = lambda x: " -  " if x is None else f"{x:.2f}"
    k = lambda x: "-inf" if x is None and b.kind == "bucket" else ("" if x is None else f"{x:g}")
    bounds = f">= {b.strike:g}" if b.kind == "threshold" else f"[{k(b.lo)}, {k(b.hi) or 'inf'})"
    bs = b.yes_bids[0].size if b.yes_bids else 0
    as_ = b.yes_asks[0].size if b.yes_asks else 0
    return (f"{b.venue:6s} {b.contract_id:32s} {b.kind:9s} {bounds:18s} "
            f"bid {px(b.best_bid)} x{bs:<9g} ask {px(b.best_ask)} x{as_:<9g} "
            f"levels {len(b.yes_bids)}/{len(b.yes_asks)}  exp {b.expiry:%m-%d %H:%M}  upd {b.updated:%H:%M:%S}")


def fmt_stats(client) -> str:
    s = client.stats
    return f"{client.name}: requests={s.requests} http_429={s.http_429} errors={s.errors}"
