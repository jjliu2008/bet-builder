"""Deribit-implied fair value for threshold and range bets.

Pipeline:
1. Pull Deribit's option summary (`mark_iv`, `underlying_price` per option).
2. For each Deribit expiry (08:00 UTC), keep OTM options only and compute
   k = ln(K/F) and total variance w = (mark_iv/100)^2 * tau.
3. Fit raw SVI: w(k) = a + b * (rho * (k - m) + sqrt((k - m)^2 + sigma^2)).
4. For a target expiry T, interpolate total variance linearly in time between the bracketing
   Deribit expiries at the same k. Before the first expiry, scale w_first by tau_T / tau_first.
5. P(S_T >= K) = -dC/dK, an undiscounted Black call priced off the smile, by central
   difference with h = 0.1% of K.

Costs and probabilities are in $ per $1 payout (0 to 1). Strikes and forwards are in $.
Time is in years (365-day).
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import numpy as np
from scipy.optimize import least_squares
from scipy.stats import norm

from .books import to_utc

DERIBIT_SUMMARY = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
GOOD_DEAL_THRESHOLD = 0.02
OVERPRICED_THRESHOLD = 0.02
CACHE_TTL_S = 60.0
YEAR_S = 365.0 * 86400.0
MIN_SVI_POINTS = 5
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


# ---- verdict -----------------------------------------------------------------------------
def verdict(cost: Optional[float], fair: Optional[float],
            good_threshold: float = GOOD_DEAL_THRESHOLD,
            over_threshold: float = OVERPRICED_THRESHOLD) -> Optional[str]:
    """Label a cost against fair value.

    Args:
        cost: our effective cost in $/contract, including fees.
        fair: the fair probability, in $/contract.
        good_threshold: how far below fair, in $, counts as a "good deal" (default 0.02).
        over_threshold: how far above fair, in $, counts as "overpriced" (default 0.02).

    Returns:
        "good deal" if cost <= fair - good_threshold, "overpriced" if cost >= fair + over_threshold,
        otherwise "fair". Returns None if either input is None.
    """
    if cost is None or fair is None:
        return None
    diff = cost - fair
    eps = 1e-9
    if diff <= -good_threshold + eps:
        return "good deal"
    if diff >= over_threshold - eps:
        return "overpriced"
    return "fair"


# ---- SVI + Black -------------------------------------------------------------------------
def svi(k, a: float, b: float, rho: float, m: float, sigma: float):
    """Return raw SVI total variance w(k) = a + b * (rho * (k - m) + sqrt((k - m)^2 + sigma^2)).

    Args:
        k: log-moneyness ln(K/F), a scalar or array.
        a, b, rho, m, sigma: raw SVI parameters (b >= 0, |rho| < 1, sigma > 0).

    Returns:
        Total variance (implied vol^2 * tau), with the same shape as k.
    """
    k = np.asarray(k, dtype=float)
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + sigma ** 2))


def fit_svi(k: Sequence[float], w: Sequence[float], weights: Optional[Sequence[float]] = None) -> np.ndarray:
    """Fit raw SVI to (k, w) points by bounded least squares, trying several starting points.

    Args:
        k: log-moneyness values.
        w: total variances at those k values (> 0).
        weights: optional per-point weights (default: all 1). `build_surface` weights points
            near the money, because that is where the listed prediction-market strikes are.

    Returns:
        An array [a, b, rho, m, sigma] for the best fit found.
    """
    k = np.asarray(k, float)
    w = np.asarray(w, float)
    wt = np.ones_like(w) if weights is None else np.asarray(weights, float)
    wmax, kspan = float(w.max()), float(max(np.ptp(k), 1e-4))
    lb = [-wmax, 0.0, -0.999, k.min() - kspan, 1e-4]
    ub = [wmax * 2, 10 * wmax / kspan + 1e-6, 0.999, k.max() + kspan, 2 * kspan + 1e-3]

    def resid(p):
        return wt * (svi(k, *p) - w) / wmax

    best, best_cost = None, np.inf
    for rho0 in (-0.5, 0.0, 0.3):
        for m0 in (0.0, float(k[np.argmin(w)])):
            for s0 in (0.1 * kspan, 0.5 * kspan):
                b0 = max((w.max() - w.min()) / kspan, 1e-6)
                p0 = np.clip([max(w.min() - b0 * s0, -wmax * 0.9), b0, rho0, m0, max(s0, 2e-4)],
                             np.array(lb) + 1e-12, np.array(ub) - 1e-12)
                try:
                    r = least_squares(resid, p0, bounds=(lb, ub), method="trf", max_nfev=2000)
                except ValueError:
                    continue
                if r.cost < best_cost:
                    best, best_cost = r.x, r.cost
    return best


def black_call(F: float, K, w):
    """Return an undiscounted Black call price on the forward.

    Args:
        F: the forward in $.
        K: the strike in $ (scalar or array).
        w: total variance sigma^2 * tau at K (scalar or array).

    Returns:
        The call value in $ per 1 unit of underlying, undiscounted.
    """
    K = np.asarray(K, float)
    sw = np.sqrt(np.maximum(np.asarray(w, float), 1e-12))
    d1 = (np.log(F / K) + 0.5 * sw ** 2) / sw
    return F * norm.cdf(d1) - K * norm.cdf(d1 - sw)


# ---- surface -----------------------------------------------------------------------------
@dataclass
class Slice:
    """One Deribit expiry's fitted smile.

    Attributes:
        expiry: the settlement time (UTC; Deribit uses 08:00).
        tau: the time to expiry in years when the surface was built.
        forward: Deribit's `underlying_price` for this expiry, in $.
        k, w: the OTM points the fit used (log-moneyness, total variance).
        params: SVI [a, b, rho, m, sigma], or None if there were too few points.
            With no fit, w(k) is linearly interpolated with flat ends.
    """
    expiry: datetime
    tau: float
    forward: float
    k: np.ndarray
    w: np.ndarray
    params: Optional[np.ndarray] = None

    def total_variance(self, k) -> np.ndarray:
        """Return total variance at log-moneyness k (relative to this slice's forward)."""
        if self.params is not None:
            return np.maximum(svi(k, *self.params), 1e-10)
        order = np.argsort(self.k)
        return np.interp(k, self.k[order], self.w[order])


@dataclass
class Surface:
    """A set of SVI slices that can be interpolated to any target expiry.

    Attributes:
        asset: e.g. "BTC".
        built_at: the "now" used for every tau (UTC).
        slices: sorted by expiry.
    """
    asset: str
    built_at: datetime
    slices: List[Slice] = field(default_factory=list)

    def tau(self, expiry: datetime) -> float:
        """Return the years from `built_at` to `expiry`, floored at 1 second."""
        return max((to_utc(expiry) - self.built_at).total_seconds(), 1.0) / YEAR_S

    def forward_and_variance(self, expiry: datetime | str, strikes) -> tuple:
        """Return (F_T in $, total variance at each strike) for a target expiry.

        Between two Deribit expiries, both F and w (at the same k = ln(K/F_T)) are interpolated
        linearly in tau. Before the first expiry, w_first is scaled by tau_T/tau_first. After the
        last expiry, w_last is scaled by tau_T/tau_last.
        """
        if not self.slices:
            raise ValueError(f"no Deribit slices for {self.asset}")
        tT = self.tau(to_utc(expiry))
        K = np.asarray(strikes, float)
        s = self.slices
        if tT <= s[0].tau or len(s) == 1:
            ref = s[0] if tT <= s[0].tau else s[-1]
            F = ref.forward
            return F, ref.total_variance(np.log(K / F)) * (tT / ref.tau)
        if tT >= s[-1].tau:
            F = s[-1].forward
            return F, s[-1].total_variance(np.log(K / F)) * (tT / s[-1].tau)
        i = next(j for j in range(1, len(s)) if s[j].tau >= tT)
        a, b = s[i - 1], s[i]
        x = (tT - a.tau) / (b.tau - a.tau)
        F = a.forward + x * (b.forward - a.forward)
        k = np.log(K / F)
        return F, a.total_variance(k) + x * (b.total_variance(k) - a.total_variance(k))

    def call(self, expiry, strikes) -> np.ndarray:
        """Return undiscounted Black call prices ($) at each strike for the target expiry, priced off the smile."""
        F, w = self.forward_and_variance(expiry, strikes)
        return black_call(F, strikes, w)

    def prob_above(self, expiry, strikes, h_frac: float = 0.001) -> np.ndarray:
        """Return P(S_T >= K) = -dC/dK by central difference (h = h_frac * K), clipped to [0, 1]."""
        K = np.asarray(strikes, float)
        h = h_frac * K
        d = -(self.call(expiry, K + h) - self.call(expiry, K - h)) / (2 * h)
        return np.clip(d, 0.0, 1.0)


def _parse_instrument(name: str):
    """Parse 'BTC-20NOV26-64000-P' into (08:00 UTC expiry datetime, strike, 'C' or 'P'). Returns None if malformed."""
    try:
        _, d, k, cp = name.split("-")
        day, mon, yr = int(d[:-5]), _MONTHS[d[-5:-2]], 2000 + int(d[-2:])
        return datetime(yr, mon, day, 8, 0, tzinfo=timezone.utc), float(k), cp
    except (ValueError, KeyError):
        return None


def build_surface(rows: Iterable[dict], asset: str = "BTC", now: Optional[datetime] = None) -> Surface:
    """Fit an SVI smile per expiry from Deribit book-summary rows.

    Args:
        rows: dicts with `instrument_name`, `mark_iv` (vol, in percent), and `underlying_price` ($),
            as returned by Deribit's get_book_summary_by_currency.
        asset: a label for the surface.
        now: the valuation time (default: now, UTC).

    Returns:
        A Surface with one Slice per expiry that has at least 2 usable OTM points.
    """
    now = to_utc(now) if now else datetime.now(timezone.utc)
    groups: Dict[datetime, list] = {}
    for r in rows:
        p = _parse_instrument(r.get("instrument_name", ""))
        iv, F = r.get("mark_iv"), r.get("underlying_price")
        if not p or not iv or not F or iv <= 0 or F <= 0:
            continue
        exp, K, cp = p
        if (cp == "C" and K < F) or (cp == "P" and K >= F):      # OTM only
            continue
        groups.setdefault(exp, []).append((K, float(iv), float(F)))
    slices = []
    for exp in sorted(groups):
        tau = (exp - now).total_seconds() / YEAR_S
        pts = groups[exp]
        if tau <= 0 or len(pts) < 2:
            continue
        F = float(np.median([f for _, _, f in pts]))
        K = np.array([p[0] for p in pts])
        k = np.log(K / F)
        w = (np.array([p[1] for p in pts]) / 100.0) ** 2 * tau
        atm_sd = float(np.sqrt(w[np.argmin(np.abs(k))]))      # ~1 standard deviation in k
        weights = 1.0 / (1.0 + (k / (2.0 * atm_sd)) ** 2)      # emphasize the near-money smile
        params = fit_svi(k, w, weights) if len(pts) >= MIN_SVI_POINTS else None
        slices.append(Slice(expiry=exp, tau=tau, forward=F, k=k, w=w, params=params))
    return Surface(asset=asset.upper(), built_at=now, slices=slices)


# ---- fetching + cache --------------------------------------------------------------------
def fetch_deribit_rows(asset: str = "BTC") -> List[dict]:
    """Fetch Deribit's public option book summaries for `asset`. No key is needed."""
    import requests
    r = requests.get(DERIBIT_SUMMARY, params={"currency": asset.upper(), "kind": "option"},
                     headers={"User-Agent": "bet-builder/0.1"}, timeout=15)
    r.raise_for_status()
    return r.json()["result"]


_cache: Dict[str, tuple] = {}
_lock = threading.Lock()


def get_surface(asset: str = "BTC", fetcher: Optional[Callable[[str], List[dict]]] = None,
                ttl: float = CACHE_TTL_S, clock: Callable[[], float] = time.monotonic) -> Surface:
    """Return the fitted surface for `asset`. It is refit at most once per `ttl` seconds (default 60).

    Args:
        asset: e.g. "BTC".
        fetcher: optional. Takes an asset and returns Deribit rows (default: the live Deribit endpoint).
        ttl: the cache lifetime in seconds.
        clock: a monotonic clock, injectable for tests.
    """
    key = asset.upper()
    with _lock:
        hit = _cache.get(key)
        if hit and clock() - hit[0] < ttl:
            return hit[1]
    surf = build_surface((fetcher or fetch_deribit_rows)(key), key)
    with _lock:
        _cache[key] = (clock(), surf)
    return surf


def clear_cache() -> None:
    """Drop every cached surface."""
    with _lock:
        _cache.clear()


# ---- public fair-value API ---------------------------------------------------------------
def fair_above(asset: str, expiry: datetime | str, strikes: Iterable[float],
               surface: Optional[Surface] = None) -> Dict[float, float]:
    """Return the options-implied probability that S_T >= K, for each strike.

    Args:
        asset: e.g. "BTC".
        expiry: the bet's settlement time (ISO string or datetime, UTC).
        strikes: strikes in $.
        surface: optional prebuilt Surface (default: the cached live surface).

    Returns:
        {strike: probability in [0, 1]}. Values are forced to be non-increasing in K, which
        guards against small butterfly-arbitrage wiggles in the fit.
    """
    surf = surface or get_surface(asset)
    ks = sorted({float(k) for k in strikes})
    if not ks:
        return {}
    p = np.minimum.accumulate(surf.prob_above(to_utc(expiry), ks))
    return {k: float(v) for k, v in zip(ks, p)}


def fair_below(asset: str, expiry, strikes, surface: Optional[Surface] = None) -> Dict[float, float]:
    """Return P(S_T < K) = 1 - P(S_T >= K) for each strike in $, as {strike: probability}."""
    return {k: 1.0 - v for k, v in fair_above(asset, expiry, strikes, surface).items()}


def fair_range(asset: str, expiry, lo: float, hi: float, surface: Optional[Surface] = None) -> float:
    """Return P(lo <= S_T < hi) = above(lo) - above(hi), with strikes in $."""
    p = fair_above(asset, expiry, [lo, hi], surface)
    return max(p[float(lo)] - p[float(hi)], 0.0)


def fair_for_request(request, surface: Optional[Surface] = None) -> Optional[float]:
    """Return the fair probability of a RouteRequest's outcome. It can be used as `price_route(..., fair_fn=...)`.

    Args:
        request: an engine.routes.RouteRequest.
        surface: optional prebuilt Surface.

    Returns:
        The probability in [0, 1], or None if no surface is available, e.g. if Deribit is
        down or the asset isn't listed.
    """
    try:
        if request.type == "above":
            return fair_above(request.asset, request.expiry, [request.strike], surface)[float(request.strike)]
        if request.type == "below":
            return fair_below(request.asset, request.expiry, [request.strike], surface)[float(request.strike)]
        return fair_range(request.asset, request.expiry, request.lo, request.hi, surface)
    except (ValueError, KeyError, OSError):   # requests' errors subclass OSError
        return None


def make_fair_fn(surface: Surface) -> Callable:
    """Return a fair_fn for price_route that is bound to one Surface (consistent within a request batch)."""
    return lambda request: fair_for_request(request, surface)
