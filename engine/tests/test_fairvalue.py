from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from scipy.stats import norm

from engine import fairvalue as fv
from engine.routes import RouteRequest

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
F = 65000.0


def deribit_rows(vol_fn, expiries=("17NOV26", "20NOV26", "27NOV26"), fwd=F):
    rows = []
    for e in expiries:
        for K in range(50000, 82000, 1000):
            for cp in "CP":
                k = np.log(K / fwd)
                rows.append({"instrument_name": f"BTC-{e}-{K}-{cp}", "mark_iv": 100 * vol_fn(k),
                             "underlying_price": fwd})
    return rows


def flat(v=0.5):
    return lambda k: v


def bs_digital(K, tau, vol, fwd=F):
    sw = vol * np.sqrt(tau)
    return norm.cdf((np.log(fwd / K) - 0.5 * sw ** 2) / sw)


def test_parse_instrument():
    exp, K, cp = fv._parse_instrument("BTC-20NOV26-64000-P")
    assert exp == datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc) and K == 64000 and cp == "P"
    assert fv._parse_instrument("BTC-PERPETUAL") is None


def test_svi_fit_recovers_smile():
    true = (0.002, 0.02, -0.3, 0.01, 0.05)
    k = np.linspace(-0.25, 0.25, 25)
    p = fv.fit_svi(k, fv.svi(k, *true))
    assert np.max(np.abs(fv.svi(k, *p) - fv.svi(k, *true))) < 2e-5


def test_flat_vol_matches_black_digital_between_expiries():
    surf = fv.build_surface(deribit_rows(flat(0.5)), now=NOW)
    assert len(surf.slices) == 3 and all(s.params is not None for s in surf.slices)
    T = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)     # between 20NOV 08:00 and 27NOV
    tau = (T - NOW).total_seconds() / fv.YEAR_S
    strikes = [60000, 64000, 65000, 66000, 70000]
    got = fv.fair_above("BTC", T, strikes, surface=surf)
    for K in strikes:
        assert got[K] == pytest.approx(bs_digital(K, tau, 0.5), abs=2e-3)


def test_before_first_expiry_scales_variance():
    surf = fv.build_surface(deribit_rows(flat(0.6)), now=NOW)
    T = NOW + timedelta(hours=6)                                # before 17NOV 08:00
    tau = 6 / (365 * 24)
    got = fv.fair_above("BTC", T, [66000], surface=surf)[66000]
    assert got == pytest.approx(bs_digital(66000, tau, 0.6), abs=2e-3)


def test_monotone_and_range_identities():
    skew = lambda k: 0.5 - 0.4 * k + 1.5 * k * k
    surf = fv.build_surface(deribit_rows(skew), now=NOW)
    T = "2026-11-20T22:00"
    ks = list(range(55000, 76000, 500))
    p = fv.fair_above("BTC", T, ks, surface=surf)
    vals = [p[k] for k in ks]
    assert all(a >= b for a, b in zip(vals, vals[1:]))
    assert all(0 <= v <= 1 for v in vals)
    below = fv.fair_below("BTC", T, [66000], surface=surf)[66000]
    assert below == pytest.approx(1 - p[66000])
    assert fv.fair_range("BTC", T, 64000, 66000, surface=surf) == pytest.approx(p[64000] - p[66000])
    r = RouteRequest(expiry=T, type="between", lo=64000, hi=66000)
    assert fv.fair_for_request(r, surf) == pytest.approx(p[64000] - p[66000])


def test_cache_refits_after_ttl():
    calls = []
    t = [0.0]

    def fetch(asset):
        calls.append(asset)
        return deribit_rows(flat())

    fv.clear_cache()
    fv.get_surface("BTC", fetcher=fetch, clock=lambda: t[0])
    t[0] = 59
    fv.get_surface("BTC", fetcher=fetch, clock=lambda: t[0])
    assert len(calls) == 1
    t[0] = 61
    fv.get_surface("BTC", fetcher=fetch, clock=lambda: t[0])
    assert len(calls) == 2
    fv.clear_cache()


def test_verdict_neutral_labels():
    v = fv.verdict(0.25, 0.31)
    assert v == {"verdict_code": "options_higher", "fair_note": fv.FAIR_NOTE,
                 "verdict_text": "Options market prices this 6¢ higher than the best venue price"}
    assert fv.verdict(0.26, 0.27)["verdict_code"] == "in_line"
    assert fv.verdict(0.26, 0.27)["verdict_text"] == "Options market prices this within 2¢ of the best venue price"
    lower = fv.verdict(0.295, 0.27)
    assert lower["verdict_code"] == "options_lower" and "2.5¢ lower" in lower["verdict_text"]
    assert fv.verdict(0.28, 0.27, lower_threshold=0.01)["verdict_code"] == "options_lower"
    assert fv.verdict(0.25, 0.27)["verdict_code"] == "options_higher"          # exactly 2¢ counts
    na = fv.verdict(None, 0.27)
    assert na["verdict_code"] == "unavailable" and na["fair_note"] is None
    assert "2 hours" in fv.verdict(0.2, 0.3, too_close=True)["verdict_text"]
    assert all(set(fv.verdict(c, 0.3)) == {"verdict_code", "verdict_text", "fair_note"} for c in (0.1, 0.3, 0.5))
