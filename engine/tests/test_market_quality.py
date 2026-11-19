"""Market-specific evidence and route/API integration; no external services."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from engine.books import cost_to_buy
from engine.market_quality import assess_market_quality
from engine.market_risk_core.fusion import RiskFusionEngine
from engine.market_risk_core.policy import PolicyEngine
from engine.routes import RouteRequest, price_route
from engine.tests.conftest import EXP_KEY, mk

NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)


def book(**updates):
    return mk("gemini", 68000, bids=[(0.49, 100)], asks=[(0.51, 100)]).model_copy(
        update={"updated": NOW, **updates})


def assess(b, size=10, stale=()):
    fill = cost_to_buy(b, "YES", size)
    return assess_market_quality([fill], size, fill.filled, stale, NOW)


def codes(result):
    return {f.code for f in result.findings}


def test_fresh_tight_book_is_clear_and_models_are_explicitly_unavailable():
    result = assess(book())
    assert result.status == "clear" and result.risk_score == 0
    assert result.advisory_only and not result.findings
    assert "market_trained_anomaly_model" in result.unavailable_signals


@pytest.mark.parametrize("age,status,code", [
    (60, "clear", None), (61, "caution", "AGING_QUOTE"),
    (120, "caution", "AGING_QUOTE"), (121, "unreliable", "OLD_QUOTE"),
    (-5, "clear", None), (-6, "review", "FUTURE_TIMESTAMP"),
])
def test_quote_age_boundaries(age, status, code):
    result = assess(book(updated=NOW - timedelta(seconds=age)))
    assert result.status == status
    assert codes(result) == ({code} if code else set())


def test_expired_crossed_and_missing_settlement_evidence():
    crossed = mk("gemini", 68000, bids=[(0.70, 100)], asks=[(0.60, 100)])
    result = assess(crossed.model_copy(update={"updated": NOW, "expiry": NOW,
                                             "settlement_index": ""}))
    assert result.status == "unreliable" and result.risk_score == 100
    assert codes(result) == {"CROSSED_BOOK", "EXPIRED_MARKET", "UNKNOWN_SETTLEMENT"}


def test_wide_spread_one_sided_and_partial_depth():
    wide = mk("gemini", 68000, bids=[(0.40, 100)], asks=[(0.50, 100)])
    assert assess(wide.model_copy(update={"updated": NOW})).status == "caution"
    result = assess(book(yes_bids=[]), size=150)
    assert result.status == "review"
    assert codes(result) == {"ONE_SIDED_BOOK", "PARTIAL_DEPTH"}


def test_empty_evidence_cannot_appear_clear():
    result = assess_market_quality([], 10, 0, [], NOW)
    assert result.status == "review" and "NO_BOOK_EVIDENCE" in codes(result)


def test_multiple_findings_keep_highest_severity_and_stale_reason():
    result = assess(book(updated=NOW - timedelta(seconds=121)), stale=["Frozen quote"])
    assert result.risk_score == 75 and result.status == "unreliable"
    assert {"OLD_QUOTE", "STALE_QUOTE_SIGNAL"} <= codes(result)


def test_split_route_settlement_evidence_and_pricing_stay_intact():
    a = book()
    b = mk("kalshi", 68000, bids=[(0.49, 100)], asks=[(0.51, 100)]).model_copy(
        update={"updated": NOW})
    req = RouteRequest(expiry=EXP_KEY, type="above", lo=68000, size=150)
    clean = price_route(req, [a, b], now=NOW)
    stale = price_route(req, [a, b], stale_fn=lambda _: "Frozen quote", now=NOW)
    assert clean.market_quality.status == "caution"
    assert "SETTLEMENT_DIFFERENCE" in codes(clean.market_quality)
    assert stale.market_quality.status == "review"
    assert stale.total_cost == clean.total_cost and stale.legs == clean.legs
    assert stale.filled_size == clean.filled_size == 150
    assert any("Market quality (review" in w for w in stale.warnings)


def test_api_exposes_assessment_without_external_calls():
    from backend.app import create_app
    from backend.ingest.poller import State

    state = State(now=lambda: NOW)
    state.set_books("gemini", [book(updated=NOW - timedelta(seconds=121))])
    state.finish_poll([EXP_KEY], duration_s=1)
    client = TestClient(create_app(state=state, chain=None))
    response = client.post("/route", json={"expiry": EXP_KEY, "type": "above", "lo": 68000})
    assert response.status_code == 200
    route = response.json()
    assert route["market_quality"]["status"] == "unreliable"
    assert route["market_quality"]["advisory_only"] is True
    assert "Market quality" in route["warnings"][-1]


@pytest.mark.parametrize("score,band", [(34, "ALLOW"), (35, "CHALLENGE"),
    (54, "CHALLENGE"), (55, "REVIEW"), (74, "REVIEW"), (75, "RESTRICT")])
def test_reused_band_boundaries(score, band):
    assert PolicyEngine().band(score) == band


def test_reused_fusion_keeps_half_up_rounding_and_rejects_nan():
    engine = RiskFusionEngine(weights={"anomaly": 0, "rules": 1,
                                       "threat_similarity": 0, "gemini_semantic": 0})
    assert engine.deterministic_score(0, 0.545, 0) == 55
    with pytest.raises(ValueError):
        engine.deterministic_score(0, float("nan"), 0)


@pytest.mark.parametrize("symbol,lo,hi", [("AAPL", 220, 230), ("NVDA", 170, 180)])
def test_stock_scenario_isolated_from_btc_feed(symbol, lo, hi):
    from backend.app import create_app
    from backend.ingest.poller import State

    client = TestClient(create_app(state=State(now=lambda: NOW)))
    expiry, = client.get("/expiries", params={"asset": symbol}).json()
    result = client.post("/route", json={"asset": symbol, "expiry": expiry,
        "type": "between", "lo": lo, "hi": hi, "size": 10})
    assert result.status_code == 200, result.text
    route = result.json()
    assert all(symbol in leg["contract"] for leg in route["legs"])
    assert route["market_quality"]["status"] == "clear"
    assert route["fair_value"] is None and "synthetic" in route["warnings"][-1]
    assert client.get("/expiries?asset=BTC").status_code == 503


def test_divergence_requires_matching_settlement_and_fresh_peer():
    selected = book()
    peer = mk("kalshi", 68000, bids=[(0.69, 20)], asks=[(0.71, 20)]).model_copy(
        update={"updated": NOW, "settlement_index": selected.settlement_index})
    fill = cost_to_buy(selected, "YES", 10)
    for other, expected in [(peer, True), (peer.model_copy(update={"asset": "AAPL"}), False),
                            (peer.model_copy(update={"updated": NOW-timedelta(minutes=3)}), False)]:
        quality = assess_market_quality([fill], 10, 10, [], NOW, [other])
        assert ("VENUE_DIVERGENCE" in codes(quality)) == expected


def test_depth_slippage_uses_average_fill_price():
    b = mk("gemini", 68000, bids=[(0.48, 100)], asks=[(0.50, 1), (0.62, 20)])
    assert "DEPTH_SLIPPAGE" in codes(assess(b.model_copy(update={"updated": NOW})))


def test_crossed_book_exclusion_reprices_against_remaining_depth():
    from engine.routes import RouteConfig, RouteError

    crossed = mk("gemini", 68000, bids=[(0.70, 50)], asks=[(0.30, 50)])
    valid = mk("kalshi", 68000, bids=[(0.46, 50)], asks=[(0.50, 50)])
    req = RouteRequest(expiry=EXP_KEY, type="above", lo=68000, size=10)
    config = RouteConfig(exclude_crossed_books=True)
    route = price_route(req, [crossed, valid], config=config, now=NOW)
    assert [leg.venue for leg in route.legs] == ["kalshi"]
    assert any("Excluded crossed book" in w for w in route.warnings)
    with pytest.raises(RouteError, match="Every matching book"):
        price_route(req, [crossed], config=config, now=NOW)


def test_historical_shift_needs_five_prior_observations():
    b = book()
    fill = cost_to_buy(b, "YES", 10)
    points = [(NOW - timedelta(seconds=i), 0.25) for i in range(1, 6)]
    for history, expected in [(points, True), (points[1:], False), (points[:1]*5, False)]:
        result = assess_market_quality([fill], 10, 10, [], NOW, history={b.contract_id: history})
        assert ("MIDPOINT_SHIFT" in codes(result)) == expected


def test_api_policy_requires_compatible_settlement_for_synthetic_payoff():
    from engine.routes import RouteConfig, RouteError

    a = mk("gemini", 66000, bids=[(.65, 50)], asks=[(.67, 50)])
    b = mk("kalshi", 68000, bids=[(.45, 50)], asks=[(.47, 50)])
    req = RouteRequest(expiry=EXP_KEY, type="between", lo=66000, hi=68000)
    with pytest.raises(RouteError, match="compatible settlement"):
        price_route(req, [a, b], config=RouteConfig(require_common_settlement=True))
