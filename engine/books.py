"""Normalized order books shared by every venue.

Each book is stored in YES terms:
- `yes_bids` are sorted best (highest) first.
- `yes_asks` are sorted best (lowest) first.
- Prices are in $/contract, in [0, 1]. Sizes are in contracts.

Buying NO at price 1 - b fills against a YES bid at b, so NO asks are derived from the YES
bids rather than stored.

The outcome a YES contract pays on is [lo, hi), where None means unbounded:
- threshold "S >= K": lo = K, hi = None
- range bucket:       lo = a, hi = b
- lower tail "S < K":  lo = None, hi = K
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from .fees import order_fee

Side = Literal["YES", "NO"]
Kind = Literal["threshold", "bucket"]


def to_utc(dt: datetime | str) -> datetime:
    """Parse or normalize a timestamp to an aware UTC datetime.

    Args:
        dt: a datetime (naive values are treated as UTC) or an ISO string (a trailing 'Z' is ok).

    Returns:
        A timezone-aware UTC datetime.
    """
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def expiry_key(dt: datetime | str) -> str:
    """Format an expiry the way the API contract does: ISO UTC to the minute, e.g. '2026-11-20T22:00'.

    Args:
        dt: a datetime or ISO string.

    Returns:
        A string of the form 'YYYY-MM-DDTHH:MM' (UTC, no offset suffix).
    """
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M")


def iso(dt: datetime) -> str:
    """Format a datetime as an ISO 8601 UTC string with a 'Z' suffix, to the second."""
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


class Level(BaseModel):
    """One price level of a book.

    Attributes:
        price: $/contract, in [0, 1].
        size: the number of contracts resting at that price (> 0).
    """
    price: float = Field(ge=0.0, le=1.0)
    size: float = Field(gt=0.0)


class Book(BaseModel):
    """A normalized YES order book for one contract on one venue.

    Attributes:
        venue: "gemini" or "kalshi".
        contract_id: the venue symbol, e.g. 'GEMI-BTC2611202200-HI68000' or 'KXBTCD-26NOV2017-T67999.99'.
        strike: the threshold strike K in $ (YES pays if S_T >= K). For buckets it equals `lo`.
        expiry: the settlement time, as an aware UTC datetime.
        kind: "threshold" or "bucket".
        lo, hi: the YES outcome is lo <= S_T < hi, in $. None means unbounded.
        yes_bids: YES bids, best (highest) first.
        yes_asks: YES asks, best (lowest) first.
        updated: when this quote was observed or last changed (UTC).
        settlement_index: a human-readable name of the index the contract settles on.
        link: a URL to the contract on the venue.
        asset: the underlying, e.g. "BTC".
    """
    venue: str
    contract_id: str
    strike: Optional[float] = None
    expiry: datetime
    kind: Kind = "threshold"
    lo: Optional[float] = None
    hi: Optional[float] = None
    yes_bids: List[Level] = Field(default_factory=list)
    yes_asks: List[Level] = Field(default_factory=list)
    updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    settlement_index: str = ""
    link: str = ""
    asset: str = "BTC"

    @field_validator("expiry", "updated", mode="before")
    @classmethod
    def _utc(cls, v):
        return to_utc(v)

    @model_validator(mode="after")
    def _normalize(self):
        self.yes_bids = sorted((l for l in self.yes_bids if l.size > 0), key=lambda l: -l.price)
        self.yes_asks = sorted((l for l in self.yes_asks if l.size > 0), key=lambda l: l.price)
        if self.kind == "threshold":
            if self.strike is None:
                self.strike = self.lo
            if self.lo is None:
                self.lo = self.strike
            if self.strike is None:
                raise ValueError("threshold book needs a strike")
        elif self.strike is None:
            self.strike = self.lo
        return self

    # ---- quote helpers -------------------------------------------------------------------
    def asks(self, side: Side) -> List[Level]:
        """Return the levels you can buy `side` from, cheapest first, in $/contract."""
        if side == "YES":
            return list(self.yes_asks)
        return [Level(price=round(1.0 - l.price, 6), size=l.size) for l in self.yes_bids]

    @property
    def best_bid(self) -> Optional[float]:
        """Return the best YES bid in $/contract, or None if the bid side is empty."""
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_ask(self) -> Optional[float]:
        """Return the best YES ask in $/contract, or None if the ask side is empty."""
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def mid(self) -> Optional[float]:
        """Return the YES mid, (best bid + best ask) / 2, in $/contract. None if either side is empty."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    def depth(self, side: Side) -> float:
        """Return the total visible contracts you can buy on `side`."""
        return float(sum(l.size for l in self.asks(side)))

    def outcome_bounds(self, side: Side) -> Tuple[Optional[float], Optional[float]]:
        """Return the [lo, hi) price range that buying `side` pays $1 on.

        NO is only defined for threshold books: NO on S >= K pays on S < K.
        """
        if side == "YES":
            return (self.lo, self.hi)
        if self.kind != "threshold":
            raise ValueError("NO on a bucket is not a contiguous range")
        return (None, self.strike)


class Fill(BaseModel):
    """The result of walking one book for one order.

    Attributes:
        book: the book that was walked.
        side: "YES" or "NO".
        requested: the contracts asked for.
        filled: the contracts actually fillable from visible depth (<= requested).
        avg_price: the size-weighted average fill price in $/contract, before fees (0 if nothing filled).
        notional: the sum of price * size over the levels hit, in $.
        fee: the taker fee for the whole order, in $ (rounded up per order unless disabled).
        total: notional + fee, in $.
        levels: the (price, size) pairs actually hit.
    """
    book: Book
    side: Side
    requested: float
    filled: float
    avg_price: float
    notional: float
    fee: float
    total: float
    levels: List[Tuple[float, float]]

    @property
    def complete(self) -> bool:
        """Return True if the whole requested size was fillable."""
        return self.filled >= self.requested - 1e-9


def cost_to_buy(book: Book, side: Side, size: float, round_fees: bool = True) -> Fill:
    """Walk a book's asks to buy `size` contracts of `side`, cheapest levels first.

    Args:
        book: a normalized Book.
        side: "YES" (walks `yes_asks`) or "NO" (walks the NO asks implied by `yes_bids`, at 1 - bid).
        size: the contracts wanted (> 0).
        round_fees: True rounds the order's fee up to the next cent (venue rule).
            False uses the exact fee.

    Returns:
        A Fill with avg_price ($/contract, before fees), fee ($ for the order), total ($), and
        filled (contracts; less than `size` when the book is too thin).
    """
    remaining = float(size)
    hit: List[Tuple[float, float]] = []
    for lvl in book.asks(side):
        if remaining <= 1e-12:
            break
        take = min(remaining, lvl.size)
        hit.append((lvl.price, take))
        remaining -= take
    filled = float(sum(c for _, c in hit))
    notional = float(sum(p * c for p, c in hit))
    fee = order_fee(hit, round_per_order=round_fees) if hit else 0.0
    return Fill(
        book=book, side=side, requested=float(size), filled=filled,
        avg_price=(notional / filled) if filled > 0 else 0.0,
        notional=notional, fee=fee, total=notional + fee, levels=hit,
    )
