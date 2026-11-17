"""Contract matcher: pairs Gemini and Kalshi thresholds by expiry and normalized strike.

`build_report(books)` takes every Book the poller knows about, for every expiry, and returns a
`MatchReport`. Nothing is dropped silently: a contract that isn't paired shows up as an unmatched
strike, an unmatched expiry, a duplicate, or a skipped contract, each with a reason.

Only identity fields are read (venue, expiry, kind, strike, bounds, settlement index), so the
books may be listing-only, with no order-book levels.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from pydantic import BaseModel, Field

from engine import Book, expiry_key
from engine.books import iso, to_utc

VENUES = ("gemini", "kalshi")
NAMES = {"gemini": "Gemini", "kalshi": "Kalshi"}


def normalize_strike(x: float) -> float:
    """Return the $ boundary a strike stands for: 64799.99 becomes 64800; 64800 stays 64800."""
    cents = round(float(x) * 100)
    return round((cents + 1) / 100, 2) if cents % 100 == 99 else round(float(x), 2)


def fmt_strike(k: float) -> str:
    return f"{k:.0f}" if abs(k - round(k)) < 1e-6 else f"{k:.2f}"


def _other(venue: str) -> str:
    return "kalshi" if venue == "gemini" else "gemini"


class Pair(BaseModel):
    """One strike listed as a threshold on both venues ("S_T >= strike")."""
    strike: float
    gemini: str                       # contract id
    kalshi: str                       # contract id
    gemini_index: str
    kalshi_index: str
    index_differs: bool               # True when the two sides settle on different indexes


class Unmatched(BaseModel):
    """A threshold listed on one venue only, within an expiry both venues list."""
    venue: str
    strike: float
    contract_id: str
    reason: str


class Bucket(BaseModel):
    """A range contract: YES pays if lo <= S_T < hi. None means unbounded."""
    venue: str
    contract_id: str
    lo: Optional[float] = None
    hi: Optional[float] = None
    settlement_index: str = ""


class Issue(BaseModel):
    """A contract left out of the pairing for a reason other than a missing counterpart."""
    venue: str
    contract_id: Optional[str] = None
    expiry: Optional[str] = None
    reason: str


class ExpiryMatch(BaseModel):
    """The match for one expiry that has thresholds on both venues."""
    expiry: str
    expired: bool = False
    pairs: List[Pair] = Field(default_factory=list)
    buckets: List[Bucket] = Field(default_factory=list)
    unmatched: List[Unmatched] = Field(default_factory=list)

    def unmatched_on(self, venue: str) -> List[Unmatched]:
        return [u for u in self.unmatched if u.venue == venue]


class UnmatchedExpiry(BaseModel):
    """An expiry that has thresholds on one venue only, or no thresholds at all."""
    expiry: str
    venue: str
    expired: bool = False
    strikes: List[float] = Field(default_factory=list)      # that venue's threshold strikes
    contracts: int = 0                                      # thresholds plus buckets on that venue
    reason: str


class MatchReport(BaseModel):
    """The matcher's output.

    Attributes:
        matched: expiry keys with thresholds on both venues that have not expired, ascending.
            The poller polls the nearest of these.
        expiries: {expiry key: ExpiryMatch} for every expiry on both venues, expired ones included.
        unmatched_expiries: expiries on one venue only.
        issues: duplicates and contracts the ingest could not normalize.
        warnings: conditions that make the whole report unreliable, e.g. a venue with no contracts.
    """
    asset: str = "BTC"
    built_at: str
    matched: List[str] = Field(default_factory=list)
    expiries: Dict[str, ExpiryMatch] = Field(default_factory=dict)
    unmatched_expiries: List[UnmatchedExpiry] = Field(default_factory=list)
    issues: List[Issue] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    contracts: Dict[str, int] = Field(default_factory=dict)     # input contracts per venue

    def summary(self) -> dict:
        """Return counts for /health and logs."""
        live = [self.expiries[e] for e in self.matched]
        pairs = [p for m in live for p in m.pairs]
        return {"matched_expiries": len(self.matched),
                "expired_expiries": sum(m.expired for m in self.expiries.values()),
                "pairs": len(pairs),
                "index_differs": sum(p.index_differs for p in pairs),
                "buckets": sum(len(m.buckets) for m in live),
                "unmatched_strikes": {v: sum(len(m.unmatched_on(v)) for m in live) for v in VENUES},
                "unmatched_expiries": len(self.unmatched_expiries),
                "issues": len(self.issues), "warnings": len(self.warnings)}


def build_report(books: Iterable[Book], now: Optional[datetime] = None, asset: str = "BTC",
                 skipped: Sequence[Issue] = ()) -> MatchReport:
    """Match every Gemini and Kalshi contract for `asset`.

    Args:
        books: Books from both venues, for any expiries. Other assets are ignored.
        now: the evaluation time (default: now, UTC). Expiries at or before it are marked expired.
        asset: e.g. "BTC".
        skipped: contracts the ingest could not turn into Books. They are passed through to `issues`.

    Returns:
        A MatchReport. Every input contract appears in exactly one of: a pair, an expiry's
        buckets, an unmatched strike, an unmatched expiry, or an issue.
    """
    now = to_utc(now) if now else datetime.now(timezone.utc)
    asset = asset.upper()
    report = MatchReport(asset=asset, built_at=iso(now), issues=list(skipped))

    # {expiry: {venue: {strike: Book}}} and {expiry: {venue: [bucket Books]}}
    thresholds: Dict[str, Dict[str, Dict[float, Book]]] = defaultdict(lambda: {v: {} for v in VENUES})
    buckets: Dict[str, Dict[str, List[Book]]] = defaultdict(lambda: {v: [] for v in VENUES})
    counts = {v: 0 for v in VENUES}
    for b in sorted(books, key=lambda b: b.contract_id):
        if b.asset.upper() != asset:
            continue
        key = expiry_key(b.expiry)
        if b.venue not in VENUES:
            report.issues.append(Issue(venue=b.venue, contract_id=b.contract_id, expiry=key,
                                       reason=f"Unknown venue '{b.venue}'"))
            continue
        counts[b.venue] += 1
        if b.kind == "bucket":
            buckets[key][b.venue].append(b)
            continue
        k = normalize_strike(b.strike)
        first = thresholds[key][b.venue].get(k)
        if first is not None:
            report.issues.append(Issue(
                venue=b.venue, contract_id=b.contract_id, expiry=key,
                reason=f"{NAMES[b.venue]} lists {fmt_strike(k)} twice for {key}; "
                       f"{first.contract_id} is used and {b.contract_id} is ignored"))
            continue
        thresholds[key][b.venue][k] = b
    report.contracts = counts
    for v in VENUES:
        if counts[v] == 0:
            report.warnings.append(f"No {NAMES[v]} contracts were supplied, so nothing can be matched. "
                                   f"Its listing may be unavailable.")

    for key in sorted(set(thresholds) | set(buckets)):
        by_venue = thresholds[key] if key in thresholds else {v: {} for v in VENUES}
        expired = to_utc(key) <= now
        gem, kal = by_venue["gemini"], by_venue["kalshi"]
        if gem and kal:
            m = ExpiryMatch(expiry=key, expired=expired)
            for k in sorted(set(gem) | set(kal)):
                g, c = gem.get(k), kal.get(k)
                if g is not None and c is not None:
                    m.pairs.append(Pair(strike=k, gemini=g.contract_id, kalshi=c.contract_id,
                                        gemini_index=g.settlement_index, kalshi_index=c.settlement_index,
                                        index_differs=g.settlement_index != c.settlement_index))
                else:
                    one = g if g is not None else c
                    m.unmatched.append(Unmatched(
                        venue=one.venue, strike=k, contract_id=one.contract_id,
                        reason=f"{NAMES[one.venue]} lists {fmt_strike(k)}, {NAMES[_other(one.venue)]} "
                               f"has no {'threshold' if one.venue == 'gemini' else 'contract'} at that strike"))
            for v in VENUES:
                m.buckets += [_bucket(b) for b in sorted(buckets[key][v], key=_bucket_order)] if key in buckets else []
            report.expiries[key] = m
            if not expired:
                report.matched.append(key)
            continue
        for v in VENUES:                                     # one venue only, or buckets with no thresholds
            n_buckets = len(buckets[key][v]) if key in buckets else 0
            if not by_venue[v] and not n_buckets:
                continue
            o = NAMES[_other(v)]
            if by_venue[v]:
                reason = (f"{NAMES[v]} lists {key} ({len(by_venue[v])} strikes), "
                          f"{o} has no threshold market expiring then")
            else:
                reason = (f"{NAMES[v]} lists only range buckets for {key} ({n_buckets}), "
                          f"with no threshold on either venue")
            report.unmatched_expiries.append(UnmatchedExpiry(
                expiry=key, venue=v, expired=expired, strikes=sorted(by_venue[v]),
                contracts=len(by_venue[v]) + n_buckets, reason=reason))
    return report


def _bucket(b: Book) -> Bucket:
    return Bucket(venue=b.venue, contract_id=b.contract_id, lo=b.lo, hi=b.hi, settlement_index=b.settlement_index)


def _bucket_order(b: Book) -> tuple:
    return (b.lo if b.lo is not None else float("-inf"), b.hi if b.hi is not None else float("inf"))
