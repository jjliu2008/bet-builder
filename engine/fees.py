"""Taker fees for Gemini Predictions and Kalshi.

Both venues charge the same taker fee per order:

    fee = 0.07 * contracts * P * (1 - P)

The result is rounded UP to the next cent. P is the fill price in $/contract.
"""
from __future__ import annotations

import math
from typing import Iterable, Tuple

FEE_RATE = 0.07

# Guards against float noise, e.g. 0.07*100*0.5*0.5 = 1.7500000000000002, which would
# otherwise round up to 1.76.
_EPS = 1e-9


def round_up_cent(dollars: float) -> float:
    """Round a dollar amount up to the next whole cent.

    Args:
        dollars: an amount in $ (>= 0).

    Returns:
        The amount in $, rounded up to a multiple of 0.01. An amount that is already a whole
        cent is unchanged, within a 1e-9 float tolerance.
    """
    if dollars <= 0:
        return 0.0
    return math.ceil(dollars * 100 - _EPS) / 100


def raw_fee(price: float, contracts: float, rate: float = FEE_RATE) -> float:
    """Return the unrounded taker fee for `contracts` filled at `price`.

    Args:
        price: the fill price in $/contract, in [0, 1].
        contracts: the number of contracts (>= 0, may be fractional).
        rate: the fee coefficient (default 0.07).

    Returns:
        The fee in $, not rounded.
    """
    return rate * contracts * price * (1.0 - price)


def taker_fee(price: float, contracts: float, round_per_order: bool = True,
              rate: float = FEE_RATE) -> float:
    """Return the taker fee for one order filled at a single price.

    Args:
        price: the fill price in $/contract, in [0, 1].
        contracts: the number of contracts in the order.
        round_per_order: True (the venue rule) rounds the order's fee up to the next cent.
            False returns the exact fee, which suits per-contract comparisons.
        rate: the fee coefficient (default 0.07).

    Returns:
        The fee for the whole order, in $.
    """
    fee = raw_fee(price, contracts, rate)
    return round_up_cent(fee) if round_per_order else fee


def order_fee(fills: Iterable[Tuple[float, float]], round_per_order: bool = True,
              rate: float = FEE_RATE) -> float:
    """Return the taker fee for one order that sweeps several price levels.

    The exact per-level fees are summed and then rounded once, because rounding applies per
    order, not per level.

    Args:
        fills: (price $/contract, contracts) pairs, one per level that was hit.
        round_per_order: see `taker_fee`.
        rate: the fee coefficient.

    Returns:
        The fee for the whole order, in $.
    """
    fee = sum(raw_fee(p, c, rate) for p, c in fills)
    return round_up_cent(fee) if round_per_order else fee
