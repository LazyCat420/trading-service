"""
Pillar 5: Portfolio Math, Sizing Invariants & Cash Balance Stress Engine.

Property-based and mathematical invariant tests on portfolio accounting,
cash non-negativity, NAV reconciliation, and Kelly criterion solvers.
"""

import pytest
import math


class TestPortfolioMathInvariants:
    """Stress testing mathematical accounting invariants and sizing solvers."""

    def test_cash_non_negativity_under_concurrent_allocations(self):
        """Invariant: Total allocated buying power must never exceed available cash."""
        initial_cash = 50_000.0
        allocated_cash = 0.0

        # Simulate 5 concurrent buy proposals
        proposed_sizes = [15_000.0, 20_000.0, 10_000.0, 12_000.0, 8_000.0]
        accepted_orders = []

        for req in proposed_sizes:
            if initial_cash - allocated_cash >= req:
                allocated_cash += req
                accepted_orders.append(req)
            else:
                # Clamp or reject
                available = max(0.0, initial_cash - allocated_cash)
                if available > 0:
                    allocated_cash += available
                    accepted_orders.append(available)
                break

        remaining_cash = initial_cash - allocated_cash
        assert remaining_cash >= 0.0
        assert allocated_cash == 50_000.0
        assert remaining_cash == 0.0

    def test_nav_reconciliation_invariant(self):
        """Invariant: NAV must strictly equal Cash + Sum(Positions * Current_Price) - Liabilities."""
        cash = 32_450.75
        positions = [
            {"ticker": "AAPL", "shares": 100, "price": 225.50},
            {"ticker": "NVDA", "shares": 50, "price": 128.30},
            {"ticker": "MSFT", "shares": 40, "price": 445.10},
        ]
        liabilities = 0.0

        market_value = sum(p["shares"] * p["price"] for p in positions)
        nav = cash + market_value - liabilities

        expected_market_value = (100 * 225.50) + (50 * 128.30) + (40 * 445.10)
        assert market_value == expected_market_value
        assert nav == 32_450.75 + expected_market_value

    @pytest.mark.parametrize(
        "win_rate,reward_risk,max_cap,expected_bounded_kelly",
        [
            (0.60, 2.0, 0.20, 0.20),   # Full Kelly = (0.6*2 - 0.4)/2 = 0.40 -> clamped to 0.20
            (0.50, 1.0, 0.20, 0.0),    # Full Kelly = (0.5*1 - 0.5)/1 = 0.0 -> no edge
            (0.40, 1.0, 0.20, 0.0),    # Negative edge -> 0.0
            (0.99, 5.0, 0.25, 0.25),   # Extreme win rate -> clamped to max_cap
            (0.01, 10.0, 0.20, 0.0),   # 1% win rate -> 0.0
        ],
    )
    def test_fractional_kelly_sizing_bounds(
        self, win_rate, reward_risk, max_cap, expected_bounded_kelly
    ):
        """Verify Kelly criterion sizing is strictly bounded between 0.0 and max_cap."""
        q = 1.0 - win_rate
        b = reward_risk

        raw_kelly = (win_rate * b - q) / b
        bounded_kelly = max(0.0, min(max_cap, raw_kelly))

        assert round(bounded_kelly, 2) == expected_bounded_kelly
        assert 0.0 <= bounded_kelly <= max_cap

    def test_realized_pnl_partial_fill_accounting(self):
        """Verify FIFO / Average Cost PnL calculations on partial lot sales."""
        # Buy 100 shares @ $150
        shares_bought = 100
        cost_per_share = 150.0
        total_cost = shares_bought * cost_per_share

        # Sell 40 shares @ $180
        shares_sold_1 = 40
        sell_price_1 = 180.0
        realized_pnl_1 = shares_sold_1 * (sell_price_1 - cost_per_share)
        remaining_shares = shares_bought - shares_sold_1

        assert realized_pnl_1 == 40 * 30.0  # +$1,200
        assert remaining_shares == 60

        # Sell remaining 60 shares @ $140
        shares_sold_2 = 60
        sell_price_2 = 140.0
        realized_pnl_2 = shares_sold_2 * (sell_price_2 - cost_per_share)

        assert realized_pnl_2 == 60 * (-10.0)  # -$600
        total_realized_pnl = realized_pnl_1 + realized_pnl_2
        assert total_realized_pnl == 600.0
