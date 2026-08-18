"""Transaction costs — the component that can reverse a strategy's sign.

Context (2026-07-26): every performance number this service produced before this
module was **gross**. `fees` was 0 on all 44 live fills and `paper_trader` filled
at exactly the reference close. Chen et al. 2026 (arXiv:2603.27539) re-evaluated
FinMem's published +23% return and got **-22%** once costs were applied.

The single most dangerous bug this module could have is an inverted sign, which
would turn a cost into an alpha source and make every strategy look better the
more it traded. That is tested first and hardest.
"""
from __future__ import annotations

import math

import pytest

from app.quant import execution_costs as EC


# ── Sign convention: costs must always hurt ─────────────────────────

def test_a_buy_fills_above_the_reference_price():
    assert EC.apply_cost_to_fill(100.0, "BUY", 10.0) > 100.0


def test_a_sell_fills_below_the_reference_price():
    assert EC.apply_cost_to_fill(100.0, "SELL", 10.0) < 100.0


def test_buy_and_sell_are_symmetric_around_the_reference():
    buy = EC.apply_cost_to_fill(100.0, "BUY", 25.0)
    sell = EC.apply_cost_to_fill(100.0, "SELL", 25.0)
    assert buy - 100.0 == pytest.approx(100.0 - sell)


def test_a_round_trip_always_loses_money_at_a_flat_price():
    """Buy and immediately sell with no price move: the trader must be down by
    the round-trip cost. If this ever passes with a profit, the sign is inverted
    and every backtest above it is invalid."""
    entry = EC.apply_cost_to_fill(100.0, "BUY", 10.0)
    exit_ = EC.apply_cost_to_fill(100.0, "SELL", 10.0)
    assert exit_ < entry, "a flat round trip made money — cost sign is inverted"


@pytest.mark.parametrize("side", ["buy", "Buy", " BUY ", "BUY"])
def test_side_parsing_is_forgiving(side):
    assert EC.apply_cost_to_fill(100.0, side, 10.0) > 100.0


def test_an_unknown_side_is_treated_as_a_sell_not_a_discount():
    """Anything not recognized as a BUY must not produce a price BELOW the
    reference for a buyer — the conservative direction."""
    assert EC.apply_cost_to_fill(100.0, "GARBAGE", 10.0) <= 100.0


def test_net_return_is_lower_than_gross():
    assert EC.net_return_pct(5.0, 20.0) == pytest.approx(4.8)


def test_net_return_can_flip_a_small_gain_negative():
    """The headline risk this module exists to expose."""
    assert EC.net_return_pct(0.1, 30.0) < 0


# ── Liquidity tiers ─────────────────────────────────────────────────

def test_mega_caps_are_cheaper_than_small_caps():
    mega = EC.half_spread_bps_from_adv(10_000_000_000.0)
    small = EC.half_spread_bps_from_adv(60_000_000.0)
    assert mega < small


def test_tiers_are_monotonic_in_liquidity():
    advs = [10e9, 2e9, 500e6, 100e6, 20e6, 1e6]
    spreads = [EC.half_spread_bps_from_adv(a) for a in advs]
    assert spreads == sorted(spreads), f"tiers not monotonic: {spreads}"


def test_unknown_adv_is_none_not_a_cheap_default():
    """"Could not estimate" and "estimated as cheap" are different claims. A
    silent cheap default is how a cost model becomes a no-op."""
    assert EC.half_spread_bps_from_adv(None) is None
    assert EC.half_spread_bps_from_adv(0) is None
    assert EC.half_spread_bps_from_adv(-5) is None


def test_a_mega_cap_spread_is_plausible():
    """Sanity against reality: AAPL's true half-spread is well under 1bp. This
    pins the calibration that replaced Corwin-Schultz, which returned ~30bp for
    AAPL — a 60x overcharge that would have killed every strategy on contact."""
    assert EC.half_spread_bps_from_adv(16_000_000_000.0) <= 1.0


# ── Market impact ───────────────────────────────────────────────────

def test_impact_follows_the_square_root_law():
    """100x the order size should cost ~10x the impact."""
    small, _ = EC.market_impact_bps(1e4, 1e9, 0.02)
    large, _ = EC.market_impact_bps(1e6, 1e9, 0.02)
    assert large / small == pytest.approx(10.0, rel=0.01)


def test_impact_grows_with_participation():
    thin, _ = EC.market_impact_bps(1e6, 1e8, 0.02)
    deep, _ = EC.market_impact_bps(1e6, 1e10, 0.02)
    assert thin > deep


def test_missing_adv_is_reported_not_silently_zero():
    bps, warning = EC.market_impact_bps(1e6, None, 0.02)
    assert bps == 0.0
    assert warning, "a missing-ADV zero must carry a warning, or it reads as free"


def test_missing_volatility_is_reported_not_silently_zero():
    bps, warning = EC.market_impact_bps(1e6, 1e9, None)
    assert bps == 0.0
    assert warning


def test_an_oversized_order_is_flagged_but_still_charged():
    """Refusing to price it would UNDERSTATE a genuinely expensive trade."""
    bps, warning = EC.market_impact_bps(5e8, 1e9, 0.02)
    assert bps > 0
    assert warning and "ADV" in warning


# ── Composition ─────────────────────────────────────────────────────

def test_total_is_the_sum_of_its_parts():
    c = EC.estimate_cost_bps(
        order_value=100_000.0, quantity=500, adv_value=1e9,
        daily_volatility=0.02, commission_per_share=0.005,
    )
    assert c["total_bps"] == pytest.approx(
        c["spread_bps"] + c["impact_bps"] + c["commission_bps"]
    )


def test_adv_tier_is_preferred_over_the_blind_default():
    c = EC.estimate_cost_bps(order_value=10_000.0, adv_value=10e9, daily_volatility=0.02)
    assert c["spread_source"] == "adv_tier"
    assert c["spread_bps"] < EC.DEFAULT_HALF_SPREAD_BPS


def test_an_explicit_spread_overrides_the_tier():
    c = EC.estimate_cost_bps(order_value=10_000.0, half_spread_bps=7.5, adv_value=10e9)
    assert c["spread_source"] == "explicit"
    assert c["spread_bps"] == pytest.approx(7.5)


def test_a_fully_defaulted_cost_is_not_marked_fully_modeled():
    """A partially-modeled cost presented as complete is the same laundering
    this codebase keeps finding elsewhere."""
    c = EC.estimate_cost_bps(order_value=10_000.0)
    assert c["spread_source"] == "default"
    assert not c["fully_modeled"]


def test_a_fully_specified_cost_is_marked_fully_modeled():
    c = EC.estimate_cost_bps(
        order_value=10_000.0, adv_value=1e9, daily_volatility=0.02,
    )
    assert c["fully_modeled"]


def test_costs_are_never_negative():
    """A negative cost is a subsidy. No input should produce one."""
    for value in (1.0, 1e3, 1e6, 1e9):
        c = EC.estimate_cost_bps(order_value=value, adv_value=1e9, daily_volatility=0.02)
        assert c["total_bps"] >= 0
        assert c["spread_bps"] >= 0
        assert c["impact_bps"] >= 0


def test_a_zero_value_order_does_not_divide_by_zero():
    c = EC.estimate_cost_bps(order_value=0.0, quantity=0)
    assert c["commission_bps"] == 0.0
    assert math.isfinite(c["total_bps"])


# ── The negative control ────────────────────────────────────────────

def test_zero_cost_reproduces_the_frictionless_price_exactly():
    """The harness check: with costs zeroed, the new path must reproduce the OLD
    numbers exactly. If it does not, a later net-vs-gross difference cannot be
    attributed to costs."""
    assert EC.apply_cost_to_fill(123.45, "BUY", 0.0) == 123.45
    assert EC.apply_cost_to_fill(123.45, "SELL", 0.0) == 123.45
    assert EC.net_return_pct(3.21, 0.0) == 3.21


# ── Corwin-Schultz: kept, tested, and NOT the default ───────────────

def test_corwin_schultz_recovers_a_known_spread_without_volatility():
    """Proves the implementation is correct: a constant true price of 100 with a
    1% spread gives high=100.5/low=99.5, and the estimator must return 50bps."""
    got = EC.corwin_schultz_half_spread_bps([100.5] * 40, [99.5] * 40)
    assert got == pytest.approx(50.0, abs=1.0)


def test_corwin_schultz_needs_enough_bars():
    assert EC.corwin_schultz_half_spread_bps([100.5] * 5, [99.5] * 5) is None


def test_corwin_schultz_rejects_mismatched_input():
    assert EC.corwin_schultz_half_spread_bps([100.0] * 30, [99.0] * 20) is None


def test_corwin_schultz_survives_bad_bars():
    """Zero/negative/inverted bars are bad data, not a zero spread."""
    highs = [100.5] * 40 + [0.0, -1.0, 50.0]
    lows = [99.5] * 40 + [0.0, -2.0, 60.0]  # last bar inverted
    got = EC.corwin_schultz_half_spread_bps(highs, lows)
    assert got is None or got > 0
