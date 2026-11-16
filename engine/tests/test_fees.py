import pytest

from engine.fees import order_fee, round_up_cent, taker_fee


def test_round_up_cent_handles_float_noise():
    assert round_up_cent(0.07 * 100 * 0.5 * 0.5) == 1.75   # 1.7500000000000002 must not become 1.76
    assert round_up_cent(0.012768) == 0.02
    assert round_up_cent(0.0) == 0.0


def test_taker_fee_rounds_up_per_order():
    # 1 contract at 0.24: 0.07*0.24*0.76 = 0.012768 -> 2 cents
    assert taker_fee(0.24, 1) == 0.02
    assert taker_fee(0.24, 1, round_per_order=False) == pytest.approx(0.012768)
    # 250 contracts at 0.24: 3.192 -> 3.20 (one rounding for the whole order)
    assert taker_fee(0.24, 250) == 3.20


def test_order_fee_rounds_once_across_levels():
    fills = [(0.24, 100), (0.26, 50)]
    exact = 0.07 * (100 * 0.24 * 0.76 + 50 * 0.26 * 0.74)
    assert order_fee(fills, round_per_order=False) == pytest.approx(exact)
    assert order_fee(fills) == round_up_cent(exact)
    # rounding once per order, not per level: 3 x 0.0063 = 0.0189 -> 0.02 (per-level would be 0.03)
    assert order_fee([(0.1, 1)] * 3) == 0.02
