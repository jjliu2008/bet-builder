"""Advisory market evidence stage, evaluated after route pricing.

Scores describe visible quote quality, never profitability or fraud probability.
Wallet-trained anomaly and similarity models are deliberately disabled.
"""
from datetime import datetime
from statistics import median
from typing import List, Literal, Mapping, Sequence


from pydantic import BaseModel, Field

from .books import Book, Fill, iso, to_utc
from .market_risk_core.fusion import RiskFusionEngine
from .market_risk_core.policy import PolicyEngine

QuoteHistory = Mapping[str, Sequence[tuple[datetime, float]]]


class QualityFinding(BaseModel):
    code: str
    message: str
    score: int = Field(ge=0, le=100)
    venue: str | None = None
    contract: str | None = None


class MarketQuality(BaseModel):
    status: Literal["clear", "caution", "review", "unreliable"]
    risk_score: int = Field(ge=0, le=100)
    advisory_only: bool = True
    evaluated_at: str
    findings: List[QualityFinding] = Field(default_factory=list)
    observed_spread_cents: float | None = None
    oldest_quote_seconds: float | None = None
    methodology: str = "visible_book_rules_v3"
    unavailable_signals: List[str] = Field(default_factory=lambda: [
        "market_trained_anomaly_model", "market_threat_memory", "semantic_triage"])


def assess_market_quality(fills: Sequence[Fill], requested: float, filled: float,
                          stale_warnings: Sequence[str], now: datetime,
                          peers: Sequence[Book] = (), history: QuoteHistory | None = None) -> MarketQuality:
    """Assess only selected books; worst evidence determines the route's band.

    Evidence thresholds are provisional: age >60s caution, >120s unreliable;
    spread >=10 cents caution; crossed/expired books unreliable; missing sides,
    future timestamps, partial depth or stale-detector evidence require review.
    Missing settlement labels also require review; differing known labels caution.
    No book is excluded and no order or wallet action is performed.
    """
    now = to_utc(now)
    findings = []

    def add(code, message, score, book=None):
        findings.append(QualityFinding(code=code, message=message, score=score,
            venue=book.venue if book else None, contract=book.contract_id if book else None))

    books = {(f.book.venue, f.book.contract_id): f.book for f in fills}.values()
    indexes = set()
    for book in books:
        age = (now - to_utc(book.updated)).total_seconds()
        if age < -5:
            add("FUTURE_TIMESTAMP", "Quote timestamp is ahead of the evaluation clock.", 55, book)
        elif age > 120:
            add("OLD_QUOTE", f"Quote is {age:.0f} seconds old.", 75, book)
        elif age > 60:
            add("AGING_QUOTE", f"Quote is {age:.0f} seconds old.", 35, book)
        if to_utc(book.expiry) <= now:
            add("EXPIRED_MARKET", "The selected contract has expired.", 100, book)
        bid, ask = book.best_bid, book.best_ask
        if bid is None or ask is None:
            add("ONE_SIDED_BOOK", "Two-sided quote evidence is unavailable.", 55, book)
        elif bid > ask:
            add("CROSSED_BOOK", "The best bid exceeds the best ask.", 100, book)
        elif ask - bid >= 0.10 - 1e-9:
            add("WIDE_SPREAD", f"Visible spread is {(ask-bid)*100:.1f} cents.", 35, book)
        if book.settlement_index.strip():
            indexes.add(book.settlement_index.strip())
        else:
            add("UNKNOWN_SETTLEMENT", "Settlement index is not supplied for this book.", 55, book)
        if history and book.mid is not None and -5 <= age <= 60 and bid <= ask:
            previous = {ts: value for ts, value in history.get(book.contract_id, ())
                        if 0 < (book.updated - ts).total_seconds() <= 600 and 0 <= value <= 1}
            if len(previous) >= 5:
                baseline = median(previous.values())
                deviation = median(abs(value - baseline) for value in previous.values())
                if abs(book.mid - baseline) >= max(0.10, 4 * deviation):
                    add("MIDPOINT_SHIFT", f"Current midpoint differs from the recent median "
                        f"by {abs(book.mid-baseline)*100:.1f} cents.", 35, book)
        if book.mid is not None and bid <= ask and -5 <= age <= 60 and book.settlement_index.strip():
            for peer in peers:
                if (peer.venue == book.venue or peer.asset != book.asset or
                    (peer.expiry, peer.lo, peer.hi, peer.settlement_index) !=
                    (book.expiry, book.lo, book.hi, book.settlement_index)):
                    continue
                peer_age = (now - peer.updated).total_seconds()
                if (peer.mid is not None and peer.best_bid <= peer.best_ask and -5 <= peer_age <= 60
                        and abs(book.mid - peer.mid) >= 0.15 - 1e-9):
                    add("VENUE_DIVERGENCE", f"Midpoints differ by {abs(book.mid-peer.mid)*100:.1f} cents "
                        f"against {peer.venue} for the same settlement terms.", 55, book)
                    break
    for fill in fills:
        levels = fill.levels
        if levels and fill.avg_price - levels[0][0] >= 0.05 - 1e-9:
            add("DEPTH_SLIPPAGE", "Average fill costs at least 5 cents more than the best level.", 55, fill.book)
    if len(indexes) > 1:
        add("SETTLEMENT_DIFFERENCE", "Selected contracts use different settlement indexes.", 35)
    if filled < requested - 1e-9:
        add("PARTIAL_DEPTH", f"Visible depth fills {filled:g} of {requested:g} contracts.", 55)
    for warning in stale_warnings:
        add("STALE_QUOTE_SIGNAL", warning, 55)
    if not fills:
        add("NO_BOOK_EVIDENCE", "No selected book evidence is available.", 55)

    # Reuse validated fusion with all weight on observed market rules. Zero weights
    # prevent untrained wallet/model components from implying market evidence.
    fusion = RiskFusionEngine(weights={"anomaly": 0, "rules": 1,
        "threat_similarity": 0, "gemini_semantic": 0})
    spreads = [100 * (b.best_ask - b.best_bid) for b in books if b.mid is not None and b.best_bid <= b.best_ask]
    ages = [max(0, (now - to_utc(b.updated)).total_seconds()) for b in books]
    # Below warning thresholds, scale each measured signal up to 25 points.
    baseline = min(25, max(max(spreads, default=0) * 2.5, max(ages, default=0) * 25 / 60))
    score = fusion.deterministic_score(0, max(baseline, max((f.score for f in findings), default=0)) / 100, 0)
    status = {"ALLOW": "clear", "CHALLENGE": "caution", "REVIEW": "review",
              "RESTRICT": "unreliable"}[PolicyEngine().band(score)]
    return MarketQuality(status=status, risk_score=score, evaluated_at=iso(now), findings=findings,
                         observed_spread_cents=round(max(spreads), 2) if spreads else None,
                         oldest_quote_seconds=round(max(ages), 1) if ages else None)
