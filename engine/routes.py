"""Route engine: find the cheapest way to build a BTC bet from Gemini and Kalshi books.

Bets and the candidate builds for each:
- above K:     YES on threshold K (on either venue, or split across both), or a tiling of
               Kalshi buckets that covers [K, inf).
- below K:     NO on threshold K (either or both venues), or a bucket tiling of (-inf, K).
- between a,b: a tiling of Kalshi buckets over [a, b), or the two-leg build YES(>=a) + NO(>=b).
               The two-leg build pays $2 inside the range and $1 outside, so its effective
               cost is cash - $1.

All costs are in $ per contract of payout, including taker fees.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from .books import Book, Fill, Side, cost_to_buy, expiry_key, iso
from .fairvalue import GOOD_DEAL_THRESHOLD, OVERPRICED_THRESHOLD, verdict

BetType = Literal["above", "below", "between"]
_EPS = 1e-9


# ---- request / response models ---------------------------------------------------------
class RouteRequest(BaseModel):
    """The bet the user wants to build.

    Attributes:
        asset: the underlying, e.g. "BTC".
        expiry: the expiry, as an ISO UTC string or datetime. It is matched to the minute.
        type: "above" (S_T >= K), "below" (S_T < K), or "between" (lo <= S_T < hi).
        lo, hi: strikes in $. "above" uses lo (falling back to hi). "below" uses hi (falling back to lo).
        size: the number of contracts, i.e. the $ max payout (> 0).
    """
    asset: str = "BTC"
    expiry: str
    type: BetType
    lo: Optional[float] = None
    hi: Optional[float] = None
    size: float = Field(default=1.0, gt=0)

    @field_validator("expiry", mode="before")
    @classmethod
    def _key(cls, v):
        return expiry_key(v)

    @model_validator(mode="after")
    def _check(self):
        if self.type == "between":
            if self.lo is None or self.hi is None or not self.lo < self.hi:
                raise ValueError("'between' needs lo < hi")
        elif self.lo is None and self.hi is None:
            raise ValueError(f"'{self.type}' needs a strike in lo or hi")
        return self

    @property
    def strike(self) -> float:
        """Return the single strike K for above/below bets."""
        if self.type == "above":
            return self.lo if self.lo is not None else self.hi  # type: ignore[return-value]
        return self.hi if self.hi is not None else self.lo  # type: ignore[return-value]

    @property
    def bounds(self) -> Tuple[Optional[float], Optional[float]]:
        """Return the outcome range [lo, hi) the bet pays on. None means unbounded."""
        if self.type == "above":
            return (self.strike, None)
        if self.type == "below":
            return (None, self.strike)
        return (self.lo, self.hi)


class RouteConfig(BaseModel):
    """Tunable knobs for `price_route`.

    Attributes:
        round_fees: True rounds each order's fee up to the next cent (venue rule).
        good_threshold: $ below fair that counts as a "good deal".
        over_threshold: $ above fair that counts as "overpriced".
    """
    round_fees: bool = True
    good_threshold: float = GOOD_DEAL_THRESHOLD
    over_threshold: float = OVERPRICED_THRESHOLD


class Leg(BaseModel):
    """One order in a route. Prices and fees are in $/contract; sizes are in contracts."""
    venue: str
    contract: str
    side: Side
    price: float
    fee: float
    size_available: float
    link: str
    size: float
    fee_total: float
    strike_lo: Optional[float] = None
    strike_hi: Optional[float] = None


class Alternative(BaseModel):
    """A summary of one candidate build that was considered."""
    build: str
    venues: List[str]
    total_cost: float
    cash_outlay: float
    filled_size: float
    complete: bool


class Route(BaseModel):
    """The cheapest build for a RouteRequest. Matches POST /route in docs/api_contract.md."""
    legs: List[Leg]
    total_cost: float
    max_payout: float = 1.0
    best_single_venue_cost: Optional[float] = None
    savings: Optional[float] = None
    fair_value: Optional[float] = None
    verdict: Optional[str] = None
    stale_warnings: List[str] = Field(default_factory=list)
    mixed_settlement_index: bool = False
    timestamp: str
    build: str
    cash_outlay: float
    guaranteed_payout: float = 0.0
    size: float
    filled_size: float
    alternatives: List[Alternative] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class StrikeNotFound(ValueError):
    """Raised when a requested strike isn't listed on any venue.

    Attributes:
        strike: the strike that was requested, in $.
        nearest: the closest listed strikes, at most one below and one above, in $.
    """

    def __init__(self, strike: float, nearest: List[float], asset: str, expiry: str):
        self.strike = strike
        self.nearest = nearest
        near = ", ".join(_fmt_k(k) for k in nearest) or "none"
        super().__init__(f"Strike {_fmt_k(strike)} is not listed for {asset} {expiry}. "
                         f"Nearest listed strikes: {near}")


class RouteError(ValueError):
    """Raised when no build exists for the bet, e.g. no books or no fillable depth."""


def _fmt_k(k: float) -> str:
    return f"{k:.0f}" if abs(k - round(k)) < 1e-6 else f"{k:.2f}"


def _kk(x: Optional[float]) -> Optional[float]:
    """Return a canonical strike key in $, rounded to the cent."""
    return None if x is None else round(float(x), 2)


# ---- book walking across venues ---------------------------------------------------------
def buy_across(books: Sequence[Book], side: Side, size: float, round_fees: bool = True) -> List[Fill]:
    """Buy `size` contracts of the same payoff from one or more equivalent books, as cheaply as possible.

    Considered options:
    - each book on its own;
    - a greedy split that takes the cheapest levels across all books, sending one order per venue.

    Per-order fee rounding means a split can cost slightly more than a single-venue fill.
    Among the options that fill completely, the cheapest wins. If none fills completely, the
    greedy split wins, since it fills the most.

    Args:
        books: equivalent books, i.e. the same outcome on different venues.
        side: "YES" or "NO".
        size: the contracts wanted.
        round_fees: see `cost_to_buy`.

    Returns:
        A list of Fills, one per book used. It is empty if there is no depth at all.
    """
    books = [b for b in books if b.depth(side) > 0]
    if not books:
        return []
    options: List[List[Fill]] = [[cost_to_buy(b, side, size, round_fees)] for b in books]
    if len(books) > 1:
        levels: List[Tuple[float, float, int]] = []
        for i, b in enumerate(books):
            levels += [(l.price, l.size, i) for l in b.asks(side)]
        levels.sort(key=lambda t: t[0])
        alloc = [0.0] * len(books)
        remaining = size
        for price, lsize, i in levels:
            if remaining <= _EPS:
                break
            take = min(remaining, lsize)
            alloc[i] += take
            remaining -= take
        options.append([cost_to_buy(books[i], side, a, round_fees)
                        for i, a in enumerate(alloc) if a > _EPS])

    def filled(opt):
        return sum(f.filled for f in opt)

    def total(opt):
        return sum(f.total for f in opt)

    complete = [o for o in options if filled(o) >= size - _EPS]
    if complete:
        return min(complete, key=total)
    return max(options, key=lambda o: (filled(o), -total(o)))


class _Component(BaseModel):
    """One payoff piece of a build: buy `side` from any of these equivalent books."""
    side: Side
    books: List[Book]


class _Candidate(BaseModel):
    build: str
    components: List[_Component]
    guaranteed: float = 0.0


class _Priced(BaseModel):
    cand: _Candidate
    fills: List[Fill]
    filled: float
    cash: float          # $ per contract, incl. fees
    effective: float     # cash - guaranteed

    @property
    def venues(self) -> List[str]:
        return sorted({f.book.venue for f in self.fills})


def _price_candidate(cand: _Candidate, size: float, round_fees: bool,
                     venue: Optional[str] = None) -> Optional[_Priced]:
    comps = []
    for c in cand.components:
        books = [b for b in c.books if venue is None or b.venue == venue]
        if not books:
            return None
        comps.append((c.side, books))

    def run(n: float) -> List[List[Fill]]:
        return [buy_across(books, side, n, round_fees) for side, books in comps]

    per = run(size)
    filled = min(sum(f.filled for f in fl) for fl in per) if per else 0.0
    if filled <= _EPS:
        return None
    if filled < size - _EPS:            # thin: shrink every component to the common fillable size
        per = run(filled)
        filled = min(sum(f.filled for f in fl) for fl in per)
        if filled <= _EPS:
            return None
    fills = [f for fl in per for f in fl]
    cash = sum(f.total for f in fills) / filled
    return _Priced(cand=cand, fills=fills, filled=filled, cash=cash, effective=cash - cand.guaranteed)


# ---- candidate enumeration --------------------------------------------------------------
def _index(books: Sequence[Book], asset: str, expiry: str):
    thresholds: Dict[float, List[Book]] = {}
    buckets: List[Book] = []
    for b in books:
        if b.asset.upper() != asset.upper() or expiry_key(b.expiry) != expiry:
            continue
        if b.kind == "threshold":
            thresholds.setdefault(_kk(b.strike), []).append(b)
        else:
            buckets.append(b)
    edges = set(thresholds)
    for b in buckets:
        edges.update(k for k in (_kk(b.lo), _kk(b.hi)) if k is not None)
    return thresholds, buckets, edges


def tile_buckets(buckets: Sequence[Book], lo: Optional[float], hi: Optional[float]) -> Optional[List[Book]]:
    """Find range buckets that exactly tile [lo, hi), with no gaps or overlaps.

    Args:
        buckets: bucket books (kind == "bucket"), on any venue, for one expiry.
        lo, hi: the range in $. None means unbounded, and is matched by a tail bucket.

    Returns:
        The buckets in ascending order, or None if no exact tiling exists. When several books
        share the same bounds, the one with the cheapest YES ask is used.
    """
    by_lo: Dict[Optional[float], Book] = {}
    for b in buckets:
        key = _kk(b.lo)
        cur = by_lo.get(key)
        if cur is None or (b.best_ask or 9) < (cur.best_ask or 9):
            by_lo[key] = b
    out: List[Book] = []
    pos = _kk(lo)
    target = _kk(hi)
    for _ in range(len(by_lo) + 1):
        b = by_lo.get(pos)
        if b is None:
            return None
        out.append(b)
        pos = _kk(b.hi)
        if pos == target:
            return out
        if pos is None or (target is not None and pos > target):
            return None
    return None


def _nearest(edges, k: float) -> List[float]:
    below = [e for e in edges if e < k]
    above = [e for e in edges if e > k]
    out = []
    if below:
        out.append(max(below))
    if above:
        out.append(min(above))
    return out


def enumerate_candidates(request: RouteRequest, books: Sequence[Book]) -> List[_Candidate]:
    """List every build of `request` that the books support.

    Raises:
        StrikeNotFound: a requested strike isn't a listed threshold strike or bucket edge.
        RouteError: the strikes exist, but no build covers the bet.
    """
    thresholds, buckets, edges = _index(books, request.asset, request.expiry)
    if not edges:
        raise RouteError(f"No books for {request.asset} {request.expiry}")
    for k in [x for x in request.bounds if x is not None]:
        if _kk(k) not in edges:
            raise StrikeNotFound(k, _nearest(edges, k), request.asset, request.expiry)

    lo, hi = request.bounds
    cands: List[_Candidate] = []
    if request.type == "above" and _kk(lo) in thresholds:
        cands.append(_Candidate(build="single", components=[_Component(side="YES", books=thresholds[_kk(lo)])]))
    if request.type == "below" and _kk(hi) in thresholds:
        cands.append(_Candidate(build="single", components=[_Component(side="NO", books=thresholds[_kk(hi)])]))
    if request.type == "between" and _kk(lo) in thresholds and _kk(hi) in thresholds:
        cands.append(_Candidate(build="two_leg", guaranteed=1.0, components=[
            _Component(side="YES", books=thresholds[_kk(lo)]),
            _Component(side="NO", books=thresholds[_kk(hi)]),
        ]))
    tiling = tile_buckets(buckets, lo, hi)
    if tiling:
        cands.append(_Candidate(build="buckets",
                                components=[_Component(side="YES", books=[b]) for b in tiling]))
    if not cands:
        raise RouteError(f"Strikes are listed but no contract combination builds "
                         f"{request.type} {request.bounds} for {request.asset} {request.expiry}")
    return cands


# ---- main entry point -------------------------------------------------------------------
def _pick(priced: List[_Priced], size: float) -> Optional[_Priced]:
    if not priced:
        return None
    complete = [p for p in priced if p.filled >= size - _EPS]
    if complete:
        return min(complete, key=lambda p: p.effective)
    return max(priced, key=lambda p: (p.filled, -p.effective))


def _leg(f: Fill) -> Leg:
    lo, hi = f.book.outcome_bounds(f.side)
    return Leg(venue=f.book.venue, contract=f.book.contract_id, side=f.side,
               price=round(f.avg_price, 6), fee=round(f.fee / f.filled, 6) if f.filled else 0.0,
               size_available=f.book.depth(f.side), link=f.book.link, size=f.filled,
               fee_total=round(f.fee, 6), strike_lo=lo, strike_hi=hi)


def price_route(request: RouteRequest, books: Sequence[Book],
                fair_fn: Optional[Callable[[RouteRequest], Optional[float]]] = None,
                stale_fn: Optional[Callable[[Book], Optional[str]]] = None,
                config: Optional[RouteConfig] = None) -> Route:
    """Price the cheapest way to build `request` from `books`.

    Args:
        request: the bet (asset, expiry, type, lo/hi in $, size in contracts).
        books: normalized books for any assets or expiries. Only those matching the request are used.
        fair_fn: optional. Takes the request and returns its fair probability in $/contract, or None.
            `engine.fairvalue.fair_for_request` has this signature.
        stale_fn: optional. Takes a Book and returns a reason string if that book's quote looks
            stale, else None. Each book used by the chosen route is checked.
            `engine.stale.make_stale_fn` builds one.
        config: fee rounding and verdict thresholds.

    Returns:
        A Route. Its costs are per contract, in $, including fees. `total_cost` is the effective
        cost, which nets out the guaranteed $1 of a two-leg build. `best_single_venue_cost` is the
        cheapest complete build using only one venue.

    Raises:
        StrikeNotFound: a strike is unlisted. `.nearest` gives the closest valid strikes.
        RouteError: no build is possible, e.g. no depth.
    """
    cfg = config or RouteConfig()
    cands = enumerate_candidates(request, books)
    size = request.size

    priced = [p for c in cands if (p := _price_candidate(c, size, cfg.round_fees))]
    best = _pick(priced, size)
    if best is None:
        raise RouteError("No fillable depth for any build of this bet")

    venues = sorted({b.venue for c in cands for comp in c.components for b in comp.books})
    single: List[_Priced] = []
    for v in venues:
        for c in cands:
            p = _price_candidate(c, size, cfg.round_fees, venue=v)
            if p and p.filled >= size - _EPS:
                single.append(p)
                priced.append(p)
    best_single = min((p.effective for p in single), default=None)

    alts, seen = [], set()
    for p in sorted(priced, key=lambda p: p.effective):
        key = (p.cand.build, tuple(p.venues), round(p.effective, 6))
        if key in seen:
            continue
        seen.add(key)
        alts.append(Alternative(build=p.cand.build, venues=p.venues, total_cost=round(p.effective, 6),
                                cash_outlay=round(p.cash, 6), filled_size=p.filled,
                                complete=p.filled >= size - _EPS))

    warnings: List[str] = []
    if best.filled < size - _EPS:
        warnings.append(f"Only {best.filled:g} of {size:g} contracts are fillable from visible depth")
    if best.effective >= 1.0 - _EPS:
        warnings.append(f"Effective cost {best.effective:.4f} is at least the $1 payout: this build cannot profit")

    fair = fair_fn(request) if fair_fn else None
    stale_warnings: List[str] = []
    if stale_fn:
        for b in {id(f.book): f.book for f in best.fills}.values():
            reason = stale_fn(b)
            if reason:
                stale_warnings.append(f"{b.venue} {b.contract_id}: {reason}")

    total = round(best.effective, 6)
    return Route(
        legs=[_leg(f) for f in best.fills],
        total_cost=total,
        max_payout=1.0,
        best_single_venue_cost=None if best_single is None else round(best_single, 6),
        savings=None if best_single is None else round(best_single - best.effective, 6),
        fair_value=None if fair is None else round(fair, 6),
        verdict=verdict(total, fair, cfg.good_threshold, cfg.over_threshold),
        stale_warnings=stale_warnings,
        mixed_settlement_index=len(best.venues) > 1,
        timestamp=iso(datetime.now(timezone.utc)),
        build=best.cand.build,
        cash_outlay=round(best.cash, 6),
        guaranteed_payout=best.cand.guaranteed,
        size=size,
        filled_size=best.filled,
        alternatives=alts,
        warnings=warnings,
    )


# ---- ladder -----------------------------------------------------------------------------
def _quote(b: Optional[Book]) -> Optional[dict]:
    if b is None:
        return None
    return {"bid": b.best_bid, "ask": b.best_ask,
            "bid_size": b.yes_bids[0].size if b.yes_bids else 0.0,
            "ask_size": b.yes_asks[0].size if b.yes_asks else 0.0,
            "updated": iso(b.updated)}


def build_ladder(books: Sequence[Book], asset: str, expiry: str,
                 fair: Optional[Dict[float, float]] = None,
                 stale: Optional[Dict[float, Dict[str, Optional[str]]]] = None) -> List[dict]:
    """Build the GET /ladder payload for one asset and expiry. Only threshold books are used.

    Args:
        books: normalized books. Those not matching the asset or expiry are ignored.
        asset: e.g. "BTC".
        expiry: an expiry string or datetime, matched to the minute.
        fair: optional {strike $: P(S_T >= K)}, e.g. from `fairvalue.fair_above`.
        stale: optional {strike $: {venue: reason or None}}, e.g. from `stale.stale_flags`.

    Returns:
        A list of rows sorted by strike, shaped as in docs/api_contract.md. Venue quotes are in
        $/contract, sizes are in contracts, and `fair` is a probability.
    """
    thresholds, _, _ = _index(books, asset, expiry_key(expiry))
    rows = []
    for k in sorted(thresholds):
        by_venue = {b.venue: b for b in thresholds[k]}
        reasons = (stale or {}).get(k, {})
        f = (fair or {}).get(k)
        rows.append({
            "strike": k,
            "gemini": _quote(by_venue.get("gemini")),
            "kalshi": _quote(by_venue.get("kalshi")),
            "fair": None if f is None else round(f, 4),
            "stale": {v: bool(reasons.get(v)) for v in ("gemini", "kalshi")},
            "stale_reasons": {v: reasons.get(v) for v in ("gemini", "kalshi")},
        })
    return rows
