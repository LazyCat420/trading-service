"""
Portfolio Drawdown Unit Tests — Verify max drawdown calculations.

Tests compute_portfolio_drawdown with various P&L sequences:
  1. No trades → returns None
  2. All winning trades → drawdown is 0
  3. All losing trades → drawdown equals total loss
  4. Mixed trades → correct peak-to-trough drawdown
"""
import os
import sys
import contextlib
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


@contextlib.contextmanager
def _mongo(pnl_rows):
    """Patch the module's Mongo reader with the lot_closures P&L series.

    `compute_portfolio_drawdown` reads through `mongo_query.find_rows`, which
    returns TUPLES in the requested column order — here a single-column
    ('realized_pnl',) projection — so the fixture yields 1-tuples, not docs.
    The `db` handle the function still takes is vestigial; a cursor mock
    intercepted nothing and the test read the live book.
    """
    q = MagicMock()
    q.find_rows.side_effect = lambda coll, *a, **k: (
        [(pnl,) for pnl in pnl_rows] if coll == "lot_closures" else []
    )
    with patch("app.trading.portfolio_drawdown.mongo_query", q), \
            patch("app.tools.portfolio_tools.resolve_bot_id", return_value="test-bot"):
        yield q


def _assert_read_the_right_book(q):
    """The read must be pinned to the resolved bot and ordered by close time.

    Both were the bug this function was written around: settings.BOT_ID named a
    bot with zero closures, and an unordered series makes the peak-to-trough
    walk meaningless.
    """
    coll, filt, cols = q.find_rows.call_args[0][:3]
    assert coll == "lot_closures"
    assert filt == {"bot_id": "test-bot"}
    assert cols == ["realized_pnl"]
    assert q.find_rows.call_args[1]["sort"] == [("closed_at", 1)]


class TestComputePortfolioDrawdown:
    """compute_portfolio_drawdown should correctly calculate max drawdown."""

    def test_no_trades_returns_none(self):
        from app.trading.portfolio_drawdown import compute_portfolio_drawdown
        with _mongo([]) as q:
            result = compute_portfolio_drawdown(None, initial_cash=100_000.0)
        _assert_read_the_right_book(q)
        assert result is None

    def test_all_winning_trades_zero_drawdown(self):
        # Equity only goes up → drawdown should be 0
        pnls = [500.0, 300.0, 700.0, 200.0]
        from app.trading.portfolio_drawdown import compute_portfolio_drawdown
        with _mongo(pnls) as q:
            result = compute_portfolio_drawdown(None, initial_cash=100_000.0)
        _assert_read_the_right_book(q)
        assert result == 0.0

    def test_all_losing_trades(self):
        # Equity only goes down
        pnls = [-1000.0, -2000.0, -500.0]
        from app.trading.portfolio_drawdown import compute_portfolio_drawdown
        with _mongo(pnls) as q:
            result = compute_portfolio_drawdown(None, initial_cash=100_000.0)
        _assert_read_the_right_book(q)

        # Total loss = -3500, peak was 100,000
        # Equity at end = 96,500. DD = (96500 - 100000) / 100000 = -0.035
        assert result < 0
        assert abs(result - (-0.035)) < 0.001

    def test_mixed_trades_correct_drawdown(self):
        # Win, win, big loss, win → max drawdown during the loss
        # Start: 100k → 101k → 103k → 93k → 95k
        # Peak at 103k, trough at 93k → DD = (93k-103k)/103k ≈ -9.7%
        pnls = [1000.0, 2000.0, -10000.0, 2000.0]
        from app.trading.portfolio_drawdown import compute_portfolio_drawdown
        with _mongo(pnls) as q:
            result = compute_portfolio_drawdown(None, initial_cash=100_000.0)
        _assert_read_the_right_book(q)

        expected_dd = (93_000.0 - 103_000.0) / 103_000.0  # ≈ -0.0971
        assert result < 0
        assert abs(result - expected_dd) < 0.001

    def test_recovery_after_drawdown(self):
        # Big loss then full recovery → max drawdown still recorded
        # 100k → 80k → 120k
        pnls = [-20000.0, 40000.0]
        from app.trading.portfolio_drawdown import compute_portfolio_drawdown
        with _mongo(pnls) as q:
            result = compute_portfolio_drawdown(None, initial_cash=100_000.0)
        _assert_read_the_right_book(q)

        # Peak was 100k, trough was 80k → DD = -20%
        expected_dd = (80_000.0 - 100_000.0) / 100_000.0
        assert abs(result - expected_dd) < 0.001

    def test_single_trade_loss(self):
        pnls = [-5000.0]
        from app.trading.portfolio_drawdown import compute_portfolio_drawdown
        with _mongo(pnls) as q:
            result = compute_portfolio_drawdown(None, initial_cash=100_000.0)
        _assert_read_the_right_book(q)

        assert result == pytest.approx(-0.05, abs=0.001)

    def test_single_trade_win(self):
        pnls = [5000.0]
        from app.trading.portfolio_drawdown import compute_portfolio_drawdown
        with _mongo(pnls) as q:
            result = compute_portfolio_drawdown(None, initial_cash=100_000.0)
        _assert_read_the_right_book(q)

        assert result == 0.0
