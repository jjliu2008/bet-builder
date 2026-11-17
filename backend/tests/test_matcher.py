"""Offline tests for the contract matcher: hand-built books first, then the recorded live listings."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.ingest import gemini, kalshi
from backend.ingest.poller import catalog_from_listings
from backend.matcher import Issue, build_report, normalize_strike
from backend.tests.replay import Replay
from engine import Book, expiry_key
from engine.dev_fetch import GEMINI_INDEX, KALSHI_INDEX

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
E1, E2, E3 = "2026-11-17T22:00", "2026-11-20T22:00", "2026-11-30T22:00"


def th(venue, strike, expiry=E1, cid=None, index=None, asset="BTC"):
    return Book(venue=venue, contract_id=cid or f"{venue}-{expiry}-{strike}", strike=strike, expiry=expiry,
                kind="threshold", asset=asset,
                settlement_index=index or (GEMINI_INDEX if venue == "gemini" else KALSHI_INDEX))


def bk(lo, hi, expiry=E1, venue="kalshi"):
    return Book(venue=venue, contract_id=f"{venue}-{expiry}-B{lo}-{hi}", kind="bucket", lo=lo, hi=hi,
                expiry=expiry, settlement_index=KALSHI_INDEX)


def accounted(report):
    """Count the contracts the report places somewhere. It must equal the number supplied."""
    n = sum(2 * len(m.pairs) + len(m.buckets) + len(m.unmatched) for m in report.expiries.values())
    return n + sum(u.contracts for u in report.unmatched_expiries) + len(report.issues)


# ---- strike normalization ----------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [(64799.99, 64800), (64800, 64800), (64750, 64750),
                                      (67999.99, 68000), (64999.5, 64999.5), (99.99, 100)])
def test_normalize_strike(raw, want):
    assert normalize_strike(raw) == want


def test_pairs_by_normalized_strike():
    r = build_report([th("gemini", 64800), th("kalshi", 64799.99, cid="KXBTCD-X-T64799.99"),
                      th("gemini", 65000), th("kalshi", 65000)], NOW)
    m = r.expiries[E1]
    assert r.matched == [E1] and [p.strike for p in m.pairs] == [64800, 65000]
    assert m.pairs[0].gemini == f"gemini-{E1}-64800" and m.pairs[0].kalshi == "KXBTCD-X-T64799.99"
    assert m.unmatched == []


# ---- settlement index --------------------------------------------------------------------
def test_pair_reports_each_side_index_and_whether_they_differ():
    r = build_report([th("gemini", 65000), th("kalshi", 65000),
                      th("gemini", 66000, index="Same index"), th("kalshi", 66000, index="Same index")], NOW)
    a, b = r.expiries[E1].pairs
    assert (a.gemini_index, a.kalshi_index, a.index_differs) == (GEMINI_INDEX, KALSHI_INDEX, True)
    assert (b.gemini_index, b.kalshi_index, b.index_differs) == ("Same index", "Same index", False)
    assert r.summary()["pairs"] == 2 and r.summary()["index_differs"] == 1


# ---- unmatched strikes -------------------------------------------------------------------
def test_unmatched_strikes_carry_a_reason():
    r = build_report([th("gemini", 64750), th("gemini", 65000), th("kalshi", 65000), th("kalshi", 64700)], NOW)
    m = r.expiries[E1]
    assert [p.strike for p in m.pairs] == [65000]
    by = {(u.venue, u.strike): u for u in m.unmatched}
    assert set(by) == {("gemini", 64750), ("kalshi", 64700)}
    assert by[("gemini", 64750)].reason == "Gemini lists 64750, Kalshi has no threshold at that strike"
    assert by[("kalshi", 64700)].reason == "Kalshi lists 64700, Gemini has no contract at that strike"
    assert by[("gemini", 64750)].contract_id == f"gemini-{E1}-64750"
    assert [u.strike for u in m.unmatched_on("kalshi")] == [64700]
    assert r.summary()["unmatched_strikes"] == {"gemini": 1, "kalshi": 1}


# ---- buckets -----------------------------------------------------------------------------
def test_buckets_are_listed_per_expiry_in_order():
    r = build_report([th("gemini", 65000), th("kalshi", 65000), bk(65500, 66000), bk(None, 64000),
                      bk(64000, 64500), bk(66000, None), bk(65000, 65500, expiry=E2)], NOW)
    b = r.expiries[E1].buckets
    assert [(x.lo, x.hi) for x in b] == [(None, 64000), (64000, 64500), (65500, 66000), (66000, None)]
    assert all(x.venue == "kalshi" and x.settlement_index == KALSHI_INDEX for x in b)
    assert r.summary()["buckets"] == 4


# ---- unmatched expiries ------------------------------------------------------------------
def test_expiries_on_one_venue_only():
    r = build_report([th("gemini", 65000, E1), th("kalshi", 65000, E1),
                      th("gemini", 60000, E3), th("gemini", 65000, E3),
                      th("kalshi", 64000, E2), bk(64000, 64500, E2)], NOW)
    assert r.matched == [E1] and set(r.expiries) == {E1}
    by = {u.expiry: u for u in r.unmatched_expiries}
    assert set(by) == {E2, E3}
    assert (by[E3].venue, by[E3].strikes, by[E3].contracts) == ("gemini", [60000, 65000], 2)
    assert by[E3].reason == f"Gemini lists {E3} (2 strikes), Kalshi has no threshold market expiring then"
    assert (by[E2].venue, by[E2].strikes, by[E2].contracts) == ("kalshi", [64000], 2)   # 1 threshold + 1 bucket
    assert by[E2].reason == f"Kalshi lists {E2} (1 strikes), Gemini has no threshold market expiring then"


def test_bucket_only_expiry_is_reported():
    r = build_report([th("gemini", 65000), th("kalshi", 65000), bk(64000, 64500, E2), bk(64500, 65000, E2)], NOW)
    (u,) = r.unmatched_expiries
    assert (u.expiry, u.venue, u.contracts, u.strikes) == (E2, "kalshi", 2, [])
    assert "only range buckets" in u.reason


# ---- nothing is dropped ------------------------------------------------------------------
def test_duplicate_strike_is_an_issue_not_a_silent_drop():
    r = build_report([th("gemini", 65000), th("kalshi", 65000, cid="K-A"), th("kalshi", 64999.99, cid="K-B")], NOW)
    assert r.expiries[E1].pairs[0].kalshi == "K-A"
    (i,) = r.issues
    assert i.contract_id == "K-B" and "twice" in i.reason and "K-A is used" in i.reason


def test_skipped_and_unknown_contracts_reach_issues():
    skipped = [Issue(venue="kalshi", contract_id="KX-ODD", reason="Strike type 'custom' is not recognised")]
    r = build_report([th("gemini", 65000), th("kalshi", 65000), th("polymarket", 65000)], NOW, skipped=skipped)
    assert [i.contract_id for i in r.issues] == ["KX-ODD", f"polymarket-{E1}-65000"]
    assert "Unknown venue" in r.issues[1].reason


def test_other_assets_are_ignored():
    r = build_report([th("gemini", 65000), th("kalshi", 65000), th("gemini", 3000, asset="ETH")], NOW)
    assert r.contracts == {"gemini": 1, "kalshi": 1} and len(r.expiries[E1].pairs) == 1


def test_every_contract_is_accounted_for():
    books = [th("gemini", 64750), th("gemini", 65000), th("kalshi", 65000), th("kalshi", 64700),
             th("kalshi", 64999.99, cid="dup"), bk(64000, 64500), th("gemini", 60000, E3),
             th("kalshi", 64000, E2), bk(64000, 64500, E2)]
    r = build_report(books, NOW)
    assert accounted(r) == len(books) == sum(r.contracts.values())


def test_missing_venue_is_a_warning():
    r = build_report([th("gemini", 65000), th("gemini", 66000, E2)], NOW)
    assert r.matched == [] and len(r.unmatched_expiries) == 2
    assert any("No Kalshi contracts were supplied" in w for w in r.warnings)
    assert build_report([], NOW).warnings and build_report([], NOW).matched == []


# ---- expiry ------------------------------------------------------------------------------
def test_expired_expiry_is_kept_in_the_report_but_not_matched():
    books = [th("gemini", 65000, E1), th("kalshi", 65000, E1), th("gemini", 65000, E2), th("kalshi", 65000, E2),
             th("gemini", 65000, "2026-11-10T22:00")]
    after_e1 = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)          # exactly at expiry counts as expired
    r = build_report(books, after_e1)
    assert r.matched == [E2]
    assert r.expiries[E1].expired is True and len(r.expiries[E1].pairs) == 1
    assert r.expiries[E2].expired is False
    assert r.unmatched_expiries[0].expired is True
    assert r.summary()["expired_expiries"] == 1 and r.summary()["pairs"] == 1     # live pairs only
    assert build_report(books, after_e1 - timedelta(seconds=1)).matched == [E1, E2]


# ---- recorded live listings --------------------------------------------------------------
@pytest.fixture(scope="module")
def recorded():
    replay = Replay()

    async def listings():
        g = gemini.make_client(1e6, transport=replay.transport)
        k = kalshi.make_client(1e6, transport=replay.transport)
        return await gemini.list_contracts(g, "BTC"), await kalshi.list_markets(k, "BTC")

    gem_list, kal_list = asyncio.run(listings())
    gem, kal, gem_bad, kal_bad = catalog_from_listings(gem_list, kal_list, "BTC", replay.now)
    return replay, gem, kal, build_report(gem + kal, replay.now, "BTC", gem_bad + kal_bad)


def test_recorded_matched_expiries(recorded):
    replay, gem, kal, r = recorded
    assert r.matched == replay.meta["targets"]
    assert r.contracts == {"gemini": len(gem), "kalshi": len(kal)}
    assert r.issues == [] and r.warnings == []
    assert accounted(r) == len(gem) + len(kal)


def test_recorded_pairs(recorded):
    replay, gem, kal, r = recorded
    by_id = {b.contract_id: b for b in gem + kal}
    pairs = [(e, p) for e, m in r.expiries.items() for p in m.pairs]
    assert len(pairs) >= 10
    for e, p in pairs:
        g, k = by_id[p.gemini], by_id[p.kalshi]
        assert (g.venue, k.venue) == ("gemini", "kalshi")
        assert expiry_key(g.expiry) == expiry_key(k.expiry) == e
        assert g.strike == k.strike == p.strike
        assert p.gemini.endswith(f"-HI{p.strike:.0f}")
        assert p.kalshi.endswith(f"-T{p.strike - 0.01:.2f}")              # Kalshi's 64799.99 is our 64800
        assert (p.gemini_index, p.kalshi_index) == (GEMINI_INDEX, KALSHI_INDEX) and p.index_differs
    assert r.summary()["index_differs"] == r.summary()["pairs"] == len(pairs)


def test_recorded_daily_expiry_pairs_every_gemini_strike(recorded):
    replay, gem, kal, r = recorded
    m = r.expiries["2026-11-17T22:00"]
    listed = sorted(b.strike for b in gem if expiry_key(b.expiry) == m.expiry)
    assert [p.strike for p in m.pairs] == listed and len(listed) == 10
    assert m.unmatched_on("gemini") == []
    kal_only = m.unmatched_on("kalshi")
    assert kal_only and all(u.reason == f"Kalshi lists {u.strike:.0f}, Gemini has no contract at that strike"
                            for u in kal_only)


def test_recorded_buckets(recorded):
    replay, gem, kal, r = recorded
    for e, m in r.expiries.items():
        want = sorted(b.contract_id for b in kal if b.kind == "bucket" and expiry_key(b.expiry) == e)
        assert sorted(b.contract_id for b in m.buckets) == want and want
        los = [b.lo for b in m.buckets]
        assert los == sorted(los) and all(b.lo < b.hi for b in m.buckets)


def test_recorded_gemini_only_expiry(recorded):
    replay, gem, kal, r = recorded
    (u,) = [u for u in r.unmatched_expiries if u.venue == "gemini"]
    assert u.expiry == E3 and u.expired is False
    assert u.strikes == sorted(b.strike for b in gem if expiry_key(b.expiry) == E3) and len(u.strikes) == 12
    assert u.reason == f"Gemini lists {E3} (12 strikes), Kalshi has no threshold market expiring then"
