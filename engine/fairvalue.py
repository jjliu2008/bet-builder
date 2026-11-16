"""Deribit-implied fair value (filled out in step 3). For now this only holds `verdict`."""
from __future__ import annotations

from typing import Optional

GOOD_DEAL_THRESHOLD = 0.02
OVERPRICED_THRESHOLD = 0.02


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
