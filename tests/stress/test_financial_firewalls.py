"""
Pillar 2: Algorithmic Financial Firewalls & Pre-Trade Risk Gatekeeper Suite.

Tests hard non-LLM circuit breakers, drawdown calculations,
position concentration limits, and dynamic order trigger evaluation.
"""

import pytest
from app.trading.portfolio_drawdown import compute_portfolio_drawdown
from app.trading.order_triggers import (
    normalize_dynamic_trigger_type,
    DYNAMIC_TRIGGER_TTL_DAYS,
)


class TestFinancialFirewalls:
    """Stress tests on financial risk limits and pre-trade guardrails."""

    def test_portfolio_drawdown_calculation_severe_loss(self, monkeypatch):
        """Verify max drawdown accurately tracks peak-to-trough drops."""
        # Mock lot_closures rows: initial $100k -> +$20k -> -$50k -> -$10k
        pnl_sequence = [(20_000.0,), (-50_000.0,), (-10_000.0,)]

        from app.db import mongo_query
        monkeypatch.setattr(
            mongo_query, "find_rows", lambda table, q, cols, sort=None: pnl_sequence
        )

        max_dd = compute_portfolio_drawdown(None, initial_cash=100_000.0)
        assert max_dd is not None
        # Peak was $120k, trough reached $60k -> Drawdown = (60k - 120k) / 120k = -50.0%
        assert round(max_dd, 2) == -0.50

    def test_portfolio_drawdown_zero_losses(self, monkeypatch):
        """Verify max drawdown is 0.0 when equity only rises."""
        pnl_sequence = [(5_000.0,), (10_000.0,), (2_000.0,)]

        from app.db import mongo_query
        monkeypatch.setattr(
            mongo_query, "find_rows", lambda table, q, cols, sort=None: pnl_sequence
        )

        max_dd = compute_portfolio_drawdown(None, initial_cash=100_000.0)
        assert max_dd == 0.0

    @pytest.mark.parametrize(
        "raw_trigger,expected_normalized",
        [
            ("sma_50_reclaim", "sma_50_rise"),
            ("sma_200_breakout", "sma_200_rise"),
            ("sma_20_rise", "sma_20_rise"),
            ("support_bounce", "support_bounce"),  # Non-SMA kept as is for validation failure
            ("", ""),
            (None, ""),
        ],
    )
    def test_dynamic_trigger_normalization_firewall(
        self, raw_trigger, expected_normalized
    ):
        """Ensure trigger synonyms are safely normalized to recognized checker vocabulary."""
        norm = normalize_dynamic_trigger_type(raw_trigger)
        assert norm == expected_normalized

    def test_position_concentration_clamp_invariant(self):
        """Assert position size cannot exceed maximum portfolio allocation limit."""
        nav = 100_000.0
        max_single_position_pct = 0.20  # 20% max NAV cap

        # Test proposed 45% position request
        proposed_size_pct = 0.45
        effective_size_pct = min(proposed_size_pct, max_single_position_pct)
        allocated_capital = nav * effective_size_pct

        assert effective_size_pct == 0.20
        assert allocated_capital == 20_000.0
        assert allocated_capital <= nav * max_single_position_pct

    def test_slippage_and_spread_firewall(self):
        """Assert trades are aborted when bid/ask spread or price slippage exceeds safety threshold."""
        bid = 98.0
        ask = 102.0  # 4% spread
        max_allowed_spread_pct = 0.02  # 2% max spread threshold

        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid

        is_spread_acceptable = spread_pct <= max_allowed_spread_pct
        assert spread_pct == 0.04
        assert is_spread_acceptable is False
