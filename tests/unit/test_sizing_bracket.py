"""Sizing bracket — units must be explicit, and HRP is a ceiling not a size.

The bug this guards: the board read an HRP line reading "target weight for VZ =
19.2% of equity" and sized the order at 19.2%. Two errors compounded — HRP
weights sum to 1.0 over the INVESTED universe (not equity, and the book was 47%
cash), and a portfolio target weight is not a single order size.
"""

from __future__ import annotations

from unittest.mock import patch

from app.quant.sizing_bracket import build_sizing_bracket


def _patch_portfolio(held, cash, equity):
    return patch(
        "app.tools.portfolio_tools._current_holdings",
        return_value=(held, cash, equity),
    )


def _no_hrp():
    """HRP needs >=2 tickers and a returns matrix; disable it for tests that
    are about the other legs."""
    return patch(
        "app.quant.returns.load_returns_matrix", side_effect=RuntimeError("no data")
    )


def _no_atr():
    return patch(
        "app.quant.technical_baseline.compute_technical_baseline", return_value={}
    )


class TestUnitsAreStated:
    def test_block_states_percent_of_equity(self):
        with _patch_portfolio({"AAA": 5000.0}, 5000.0, 10000.0), _no_hrp(), _no_atr():
            out = build_sizing_bracket("BBB")
        assert "PERCENT OF PORTFOLIO EQUITY" in out

    def test_warns_hrp_is_not_an_order_size(self):
        with _patch_portfolio({"AAA": 5000.0}, 5000.0, 10000.0), _no_hrp(), _no_atr():
            out = build_sizing_bracket("BBB")
        assert "TARGET WEIGHT, not an order size" in out


class TestBindingConstraint:
    def test_names_the_binding_constraint(self):
        with _patch_portfolio({"AAA": 5000.0}, 5000.0, 10000.0), _no_hrp(), _no_atr():
            out = build_sizing_bracket("BBB")
        assert "<== BINDS" in out

    def test_cash_binds_when_nearly_broke(self):
        # 1% cash against a 10% hard cap → cash is the binding constraint.
        with _patch_portfolio({"AAA": 9900.0}, 100.0, 10000.0), _no_hrp(), _no_atr():
            out = build_sizing_bracket("BBB")
        binding = [ln for ln in out.splitlines() if "BINDS" in ln]
        assert binding and "cash" in binding[0]

    def test_zero_room_is_called_out_explicitly(self):
        with _patch_portfolio({"AAA": 10000.0}, 0.0, 10000.0), _no_hrp(), _no_atr():
            out = build_sizing_bracket("BBB")
        assert "no room for a BUY" in out

    def test_concentration_shrinks_as_position_grows(self):
        """An existing position eats its own concentration headroom."""
        with _patch_portfolio({"BBB": 2000.0}, 8000.0, 10000.0), _no_hrp(), _no_atr():
            out = build_sizing_bracket("BBB")
        # 25% cap less the 20% already held → 5% left.
        assert "concentration cap: 5.0%" in out


class TestHrpUnitsConversion:
    """The VZ regression, reproduced with round numbers.

    HRP weights sum to 1.0 over the INVESTED sleeve. With a book that is half
    cash, a 20% HRP weight is 20% of the invested half = 10% of equity. The
    original line called it "20% of equity" and the board sized accordingly.
    """

    def _run(self, ticker="VZZ"):
        import numpy as np
        import pandas as pd

        held = {"AAA": 4000.0, "BBB": 4000.0, "VZZ": 2000.0}  # 10k invested
        cash, equity = 10000.0, 20000.0                        # 50% cash
        idx = pd.RangeIndex(300)
        cols = ["AAA", "BBB", "VZZ"]
        rng = np.random.default_rng(0)
        df = pd.DataFrame(rng.normal(0, 0.01, (300, 3)), columns=cols, index=idx)

        with _patch_portfolio(held, cash, equity), _no_atr(), patch(
            "app.quant.returns.load_returns_matrix", return_value=(df, [])
        ), patch(
            "app.quant.portfolio_math.hrp_weights",
            return_value=[0.4, 0.4, 0.2],  # VZZ = 20% of INVESTED
        ):
            return build_sizing_bracket(ticker)

    def test_hrp_is_converted_to_equity_terms(self):
        out = self._run()
        # 20% of the 10k invested sleeve = 2000 = 10% of 20k equity.
        assert "20.0% of INVESTED capital = 10.0% of equity" in out

    def test_headroom_subtracts_the_existing_position(self):
        out = self._run()
        # Already holds 2000/20000 = 10% of equity, so the target is met.
        assert "already held 10.0%" in out
        assert "headroom 0.0%" in out

    def test_cash_percentage_is_reported(self):
        assert "book is 50.0% cash" in self._run()


class TestFailOpen:
    def test_no_equity_returns_empty(self):
        with _patch_portfolio({}, 0.0, 0.0):
            assert build_sizing_bracket("BBB") == ""

    def test_portfolio_failure_returns_empty_not_raises(self):
        with patch(
            "app.tools.portfolio_tools._current_holdings",
            side_effect=RuntimeError("db down"),
        ):
            assert build_sizing_bracket("BBB") == ""

    def test_atr_failure_still_produces_other_legs(self):
        with _patch_portfolio({"AAA": 5000.0}, 5000.0, 10000.0), _no_hrp(), patch(
            "app.quant.technical_baseline.compute_technical_baseline",
            side_effect=RuntimeError("no technicals"),
        ):
            out = build_sizing_bracket("BBB")
        assert "cash available" in out
