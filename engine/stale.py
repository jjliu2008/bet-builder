"""Stale-quote detector: simple rules with configurable thresholds and a human-readable reason for each flag.

A venue's quote for a threshold contract is flagged stale when its mid differs from the other
venue's mid for the same contract by more than `min_gap` (default 4¢), AND at least one of
these holds:
  (a) frozen: its mid hasn't changed in the last `frozen_window_s` (default 5 min) while BTC
      moved more than `btc_move_bp` (default 30 bp) over that window; or
  (b) off-fair: the other venue's mid is within `fair_band` (default 2¢) of the Deribit fair
      value, while this one isn't.

Quote and spot history are passed in as arguments; the backend's logger supplies them.
`History` is a small in-memory implementation for dev use and tests.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Dict, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel

from .books import Book, Level, expiry_key, to_utc
from .fairvalue import fair_hidden

Point = Tuple[datetime, float]


class StaleConfig(BaseModel):
    """Thresholds for the stale rules.

    Attributes:
        min_gap: the cross-venue mid gap, in $, above which a quote is a candidate (default 0.04).
        frozen_window_s: how long, in seconds, a mid must be unchanged for rule (a) (default 300).
        btc_move_bp: the BTC high-low range over that window, in basis points, that counts as
            "moved" for rule (a) (default 30).
        fair_band: the distance from fair, in $, that counts as "agrees with fair" for rule (b)
            (default 0.02).
        min_fair_hours: rule (b) is skipped when the contract expires in less than this many
            hours, matching the API hiding fair value there (default 2).
    """
    min_gap: float = 0.04
    frozen_window_s: float = 300.0
    btc_move_bp: float = 30.0
    fair_band: float = 0.02
    min_fair_hours: float = 2.0


class History:
    """A minimal in-memory time series store: key -> [(UTC time, value)], oldest first.

    Keys are contract ids for quote mids ($/contract) and, by convention, "spot:<ASSET>" for
    spot prices ($). Points older than `max_age_s` are pruned on write.
    """

    def __init__(self, max_age_s: float = 1800.0):
        self.max_age = timedelta(seconds=max_age_s)
        self._d: Dict[str, Deque[Point]] = defaultdict(deque)

    def record(self, key: str, value: float, ts: Optional[datetime] = None) -> None:
        """Append one observation (value in $) at time ts (default: now, UTC)."""
        ts = to_utc(ts) if ts else datetime.now(timezone.utc)
        q = self._d[key]
        q.append((ts, float(value)))
        while q and ts - q[0][0] > self.max_age:
            q.popleft()

    def record_book(self, book: Book, ts: Optional[datetime] = None) -> None:
        """Record a book's YES mid under its contract id. Skipped if the book has no two-sided mid."""
        if book.mid is not None:
            self.record(book.contract_id, book.mid, ts or book.updated)

    def record_spot(self, asset: str, price: float, ts: Optional[datetime] = None) -> None:
        """Record a spot price in $ under the key 'spot:<ASSET>'."""
        self.record(f"spot:{asset.upper()}", price, ts)

    def series(self, key: str) -> Sequence[Point]:
        """Return the stored [(time, value)] points for `key`, oldest first."""
        return list(self._d.get(key, ()))

    def as_mapping(self) -> Dict[str, Sequence[Point]]:
        """Return every series as a plain {key: [(time, value)]} dict."""
        return {k: list(v) for k, v in self._d.items()}


# ---- rules -------------------------------------------------------------------------------
def _venue(v: str) -> str:
    return {"gemini": "Gemini", "kalshi": "Kalshi"}.get(v, v)


def frozen_for(series: Sequence[Point], current: float, now: datetime, window_s: float) -> bool:
    """Return True if the mid has equalled `current` for the whole window [now - window_s, now].

    Args:
        series: [(time, mid $)] observations, in any order.
        current: the mid now, in $.
        now: the evaluation time (UTC).
        window_s: the window length in seconds.

    Returns:
        True only if the history reaches back at least `window_s` and every observation from the
        last point before the window start onward equals `current` (within 1e-9). Returns False
        when there isn't enough history to tell.
    """
    start = now - timedelta(seconds=window_s)
    pts = sorted((to_utc(t), v) for t, v in series if to_utc(t) <= now)
    before = [p for p in pts if p[0] <= start]
    if not before:
        return False
    relevant = [before[-1]] + [p for p in pts if p[0] > start]
    return all(abs(v - current) < 1e-9 for _, v in relevant)


def spot_move_bp(spot_history: Sequence[Point], now: datetime, window_s: float) -> Optional[float]:
    """Return BTC's high-low range over [now - window_s, now] in basis points of the low, or None if there are fewer than 2 points.

    The last observation before the window start is included as the window's opening price.
    """
    start = now - timedelta(seconds=window_s)
    pts = sorted((to_utc(t), v) for t, v in spot_history if to_utc(t) <= now)
    before = [p for p in pts if p[0] <= start]
    vals = ([before[-1][1]] if before else []) + [v for t, v in pts if t > start]
    if len(vals) < 2 or min(vals) <= 0:
        return None
    return (max(vals) - min(vals)) / min(vals) * 1e4


def check_quote(this: Book, other: Book, fair: Optional[float] = None,
                quote_history: Optional[Mapping[str, Sequence[Point]]] = None,
                spot_history: Optional[Sequence[Point]] = None,
                now: Optional[datetime] = None,
                config: Optional[StaleConfig] = None) -> Optional[str]:
    """Decide whether `this` venue's quote looks stale relative to `other` venue's quote for the same contract.

    Args:
        this, other: threshold Books for the same asset, expiry, and strike, on different venues.
        fair: the Deribit fair probability P(S_T >= K), in $, or None to skip rule (b). Rule (b)
            is also skipped within `config.min_fair_hours` of expiry.
        quote_history: {contract_id: [(time, mid $)]}, used for rule (a). If this book has no
            history, its `updated` timestamp is used instead: an `updated` older than the window
            counts as unchanged.
        spot_history: [(time, BTC $)], used for rule (a).
        now: the evaluation time (default: now, UTC).
        config: the thresholds.

    Returns:
        A reason string if the quote is flagged, else None. It is also None if either book has
        no two-sided mid.
    """
    cfg = config or StaleConfig()
    now = to_utc(now) if now else datetime.now(timezone.utc)
    m, o = this.mid, other.mid
    if m is None or o is None:
        return None
    gap = abs(m - o)
    if gap <= cfg.min_gap + 1e-9:
        return None
    head = (f"{_venue(this.venue)} mid {m:.3f} is {gap * 100:.1f}¢ from "
            f"{_venue(other.venue)}'s {o:.3f}")
    reasons = []

    series = (quote_history or {}).get(this.contract_id)
    if series:
        frozen = frozen_for(series, m, now, cfg.frozen_window_s)
    else:
        frozen = (now - this.updated).total_seconds() >= cfg.frozen_window_s
    move = spot_move_bp(spot_history or [], now, cfg.frozen_window_s)
    if frozen and move is not None and move > cfg.btc_move_bp:
        mins = cfg.frozen_window_s / 60
        reasons.append(f"it hasn't changed in {mins:g} min while BTC moved {move:.0f} bp")

    if fair is not None and not fair_hidden(this.expiry, now, cfg.min_fair_hours):
        d_other, d_this = abs(o - fair), abs(m - fair)
        if d_other <= cfg.fair_band + 1e-9 and d_this > cfg.fair_band + 1e-9:
            reasons.append(f"{_venue(other.venue)} is within {cfg.fair_band * 100:g}¢ of fair "
                           f"{fair:.3f} but this quote is {d_this * 100:.1f}¢ away")

    if not reasons:
        return None
    return head + "; " + " and ".join(reasons)


def stale_flags(books: Sequence[Book], expiry: Optional[str] = None,
                fair: Optional[Mapping[float, float]] = None,
                quote_history: Optional[Mapping[str, Sequence[Point]]] = None,
                spot_history: Optional[Sequence[Point]] = None,
                now: Optional[datetime] = None,
                config: Optional[StaleConfig] = None) -> Dict[float, Dict[str, Optional[str]]]:
    """Run `check_quote` for every strike listed on two venues.

    Args:
        books: normalized books. Only threshold books are compared.
        expiry: optional expiry key to restrict to. Pass it whenever `books` spans several
            expiries, because the result is keyed by strike.
        fair: {strike $: P(S_T >= K)}, e.g. from fairvalue.fair_above.
        quote_history, spot_history, now, config: see `check_quote`.

    Returns:
        {strike $: {venue: reason string or None}}. This is the `stale` argument of
        routes.build_ladder.
    """
    groups: Dict[tuple, Dict[str, Book]] = defaultdict(dict)
    for b in books:
        if b.kind != "threshold" or (expiry and expiry_key(b.expiry) != expiry_key(expiry)):
            continue
        groups[(b.asset, expiry_key(b.expiry), round(b.strike, 2))].setdefault(b.venue, b)
    out: Dict[float, Dict[str, Optional[str]]] = {}
    for (_, _, k), by_venue in groups.items():
        row: Dict[str, Optional[str]] = {v: None for v in by_venue}
        if len(by_venue) >= 2:
            venues = sorted(by_venue)
            for v in venues:
                other = next(by_venue[u] for u in venues if u != v)
                row[v] = check_quote(by_venue[v], other, (fair or {}).get(k), quote_history,
                                     spot_history, now, config)
        out[k] = row
    return out


def make_stale_fn(books: Sequence[Book], expiry: Optional[str] = None,
                  fair: Optional[Mapping[float, float]] = None,
                  quote_history: Optional[Mapping[str, Sequence[Point]]] = None,
                  spot_history: Optional[Sequence[Point]] = None,
                  now: Optional[datetime] = None,
                  config: Optional[StaleConfig] = None) -> Callable[[Book], Optional[str]]:
    """Build a `stale_fn` for routes.price_route. It takes a Book and returns a reason or None.

    Flags are computed once from the arguments, which are the same as for `stale_flags`.
    Bucket books are never flagged, because they have no cross-venue counterpart.
    """
    flags = stale_flags(books, expiry, fair, quote_history, spot_history, now, config)

    def fn(book: Book) -> Optional[str]:
        if book.kind != "threshold":
            return None
        return flags.get(round(book.strike, 2), {}).get(book.venue)

    return fn


# ---- snapshot replay ---------------------------------------------------------------------
# One JSON object per line. "type" is "book" (the default when missing), "spot", or "fair".
# See docs/api_contract.md, "Snapshot log format".

class ReplayEvent(BaseModel):
    """One stale-flag transition found during replay.

    Attributes:
        ts: the snapshot time at which the flag changed (UTC).
        asset, expiry: e.g. "BTC" and "2026-11-20T22:00".
        strike: the threshold strike in $.
        venue: the flagged venue.
        event: "raised" when the flag appears, "cleared" when it goes away, or "active" for a
            flag that is still on (only with every=True).
        reason: the detector's reason string. For "cleared", the last reason seen.
    """
    ts: datetime
    asset: str
    expiry: str
    strike: float
    venue: str
    event: str
    reason: Optional[str]


def book_to_record(book: Book, ts: Optional[datetime] = None) -> dict:
    """Serialize a Book to one snapshot-log record (a JSON-ready dict). `ts` defaults to book.updated."""
    return {
        "type": "book", "ts": iso_ms(ts or book.updated), "venue": book.venue,
        "contract_id": book.contract_id, "asset": book.asset, "expiry": expiry_key(book.expiry),
        "kind": book.kind, "strike": book.strike, "lo": book.lo, "hi": book.hi,
        "yes_bids": [[l.price, l.size] for l in book.yes_bids],
        "yes_asks": [[l.price, l.size] for l in book.yes_asks],
        "settlement_index": book.settlement_index, "link": book.link,
    }


def record_to_book(rec: Mapping) -> Book:
    """Parse a "book" snapshot record back into a Book. The record's `ts` becomes `updated`."""
    lv = lambda xs: [Level(price=float(p), size=float(s)) for p, s in (xs or []) if float(s) > 0]
    return Book(venue=rec["venue"], contract_id=rec["contract_id"], asset=rec.get("asset", "BTC"),
                expiry=rec["expiry"], kind=rec.get("kind", "threshold"), strike=rec.get("strike"),
                lo=rec.get("lo"), hi=rec.get("hi"), yes_bids=lv(rec.get("yes_bids")),
                yes_asks=lv(rec.get("yes_asks")), updated=rec["ts"],
                settlement_index=rec.get("settlement_index", ""), link=rec.get("link", ""))


def iso_ms(dt: datetime) -> str:
    """Format a UTC timestamp as ISO 8601 with milliseconds and a 'Z' suffix."""
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.") + f"{to_utc(dt).microsecond // 1000:03d}Z"


def load_snapshots(path: str) -> list:
    """Read a JSON-lines snapshot log, skipping blank lines and lines starting with '#'.

    Raises:
        ValueError: a line isn't valid JSON. The message includes the line number.
    """
    import json
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{n}: {e}") from e
    return out


def replay(records: Sequence[Mapping], config: Optional[StaleConfig] = None,
           every: bool = False) -> list:
    """Run the stale detector over a saved snapshot log, as if live.

    Records are applied in time order. After all records sharing a timestamp are applied, the
    detector runs over the latest book per contract, with `now` = that timestamp. It uses the
    quote and spot history accumulated so far and the latest fair value per strike.

    Args:
        records: parsed snapshot records (see `load_snapshots`), in any order.
        config: the stale thresholds.
        every: if True, also emit an "active" event at every evaluation where a flag stays on.

    Returns:
        A list of ReplayEvent in time order. By default only "raised" and "cleared"
        transitions are included.
    """
    recs = sorted(records, key=lambda r: to_utc(r["ts"]))
    books: Dict[str, Book] = {}
    fair: Dict[tuple, Dict[float, float]] = defaultdict(dict)
    hist = History(max_age_s=10 ** 9)
    active: Dict[tuple, str] = {}
    events = []
    i = 0
    while i < len(recs):
        ts = to_utc(recs[i]["ts"])
        while i < len(recs) and to_utc(recs[i]["ts"]) == ts:
            r = recs[i]
            kind = r.get("type", "book")
            if kind == "book":
                b = record_to_book(r)
                books[b.contract_id] = b
                hist.record_book(b, ts)
            elif kind == "spot":
                hist.record_spot(r.get("asset", "BTC"), float(r["price"]), ts)
            elif kind == "fair":
                fair[(r.get("asset", "BTC").upper(), expiry_key(r["expiry"]))][round(float(r["strike"]), 2)] = float(r["p_above"])
            i += 1
        series = hist.as_mapping()
        seen = set()
        for asset, exp in sorted({(b.asset, expiry_key(b.expiry)) for b in books.values()}):
            group = [b for b in books.values() if b.asset == asset and expiry_key(b.expiry) == exp]
            flags = stale_flags(group, exp, fair.get((asset, exp)), series,
                                series.get(f"spot:{asset}", []), ts, config)
            for k, row in flags.items():
                for venue, reason in row.items():
                    key = (asset, exp, k, venue)
                    seen.add(key)
                    prev = active.get(key)
                    if reason and not prev:
                        events.append(ReplayEvent(ts=ts, asset=asset, expiry=exp, strike=k, venue=venue,
                                                  event="raised", reason=reason))
                    elif reason and every:
                        events.append(ReplayEvent(ts=ts, asset=asset, expiry=exp, strike=k, venue=venue,
                                                  event="active", reason=reason))
                    elif prev and not reason:
                        events.append(ReplayEvent(ts=ts, asset=asset, expiry=exp, strike=k, venue=venue,
                                                  event="cleared", reason=prev))
                    if reason:
                        active[key] = reason
                    else:
                        active.pop(key, None)
        for key in [k for k in active if k not in seen]:   # contract disappeared from the log
            events.append(ReplayEvent(ts=ts, asset=key[0], expiry=key[1], strike=key[2], venue=key[3],
                                      event="cleared", reason=active.pop(key)))
    return events
