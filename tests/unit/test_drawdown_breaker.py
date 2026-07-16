"""
Drawdown Circuit Breaker Unit Tests (plan item D2).

Smoke coverage for `paper_trader._check_drawdown_breaker` — the real BUY-blocking
breaker. It is dormant in an all-HOLD regime (only fires once portfolio_snapshots
has a peak above the current value), so these tests exercise it with synthetic
peaks instead of waiting for a live drawdown.

NOTE: this is a DIFFERENT mechanism from `compute_portfolio_drawdown`
(app/trading/portfolio_drawdown.py, covered by test_portfolio_drawdown.py). The
breaker computes drawdown itself from MAX(total_value) in portfolio_snapshots and
never calls compute_portfolio_drawdown — so patching that function would test
nothing here.

Contract under test (paper_trader.py):
  - peak read from `SELECT MAX(total_value) FROM portfolio_snapshots WHERE bot_id`
  - drawdown = (portfolio_value - peak) / peak
  - fires (returns an error dict) when drawdown <= -MAX_PORTFOLIO_DRAWDOWN_PCT
  - fails OPEN (returns None) when disabled (<=0), no peak, or any DB error
  - BUY-only: SELLs never call it (asserted via the error message contract)
"""
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.trading import paper_trader


def _patch_peak(peak_value):
    """Patch paper_trader.get_db so the breaker reads `peak_value` as the peak.

    Returns a context manager patching the module-level get_db (imported with
    `from app.db.connection import get_db`, so it must be patched on
    paper_trader itself, not on app.db.connection).
    """
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    cursor.fetchone.return_value = (peak_value,) if peak_value is not None else None
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _get_db():
        yield cursor

    return patch("app.trading.paper_trader.get_db", _get_db)


class TestDrawdownBreaker:
    """_check_drawdown_breaker should block BUYs past the drawdown limit."""

    def test_fires_at_threshold_boundary(self):
        # peak 100k, value 80k → exactly -20% drawdown; limit 20% → fires (<=).
        with _patch_peak(100_000.0), \
             patch.object(paper_trader.settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20):
            result = paper_trader._check_drawdown_breaker("bot-1", 80_000.0)

        assert result is not None, "breaker must fire at exactly the -20% limit"
        assert "error" in result
        assert result["drawdown_pct"] == -20.0
        assert result["peak_value"] == 100_000.0
        # BUY-only contract: message must state SELLs are still allowed.
        assert "SELLs allowed" in result["error"]

    def test_fires_beyond_threshold(self):
        # peak 100k, value 65k → -35% drawdown; limit 20% → fires.
        with _patch_peak(100_000.0), \
             patch.object(paper_trader.settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20):
            result = paper_trader._check_drawdown_breaker("bot-1", 65_000.0)

        assert result is not None
        assert result["drawdown_pct"] == -35.0

    def test_does_not_fire_at_small_drawdown(self):
        # peak 100k, value 95k → -5% drawdown; limit 20% → allowed.
        with _patch_peak(100_000.0), \
             patch.object(paper_trader.settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20):
            result = paper_trader._check_drawdown_breaker("bot-1", 95_000.0)

        assert result is None, "a -5% drawdown must not trip a 20% breaker"

    def test_does_not_fire_when_above_peak(self):
        # New high-water mark → positive "drawdown" → allowed.
        with _patch_peak(100_000.0), \
             patch.object(paper_trader.settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20):
            result = paper_trader._check_drawdown_breaker("bot-1", 110_000.0)

        assert result is None

    def test_fails_open_when_disabled(self):
        # MAX_PORTFOLIO_DRAWDOWN_PCT <= 0 disables the breaker entirely.
        with _patch_peak(100_000.0), \
             patch.object(paper_trader.settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.0):
            result = paper_trader._check_drawdown_breaker("bot-1", 10_000.0)

        assert result is None, "disabled breaker must never block"

    def test_fails_open_with_no_snapshots(self):
        # No peak yet (empty portfolio_snapshots) → returns None, cannot block.
        with _patch_peak(None), \
             patch.object(paper_trader.settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20):
            result = paper_trader._check_drawdown_breaker("bot-1", 50_000.0)

        assert result is None

    def test_fails_open_on_db_error(self):
        # Any DB error must fail open — the breaker is a safety net, not a
        # trade dependency, so it must never block on infrastructure failure.
        def _boom():
            raise RuntimeError("db down")

        with patch("app.trading.paper_trader.get_db", _boom), \
             patch.object(paper_trader.settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20):
            result = paper_trader._check_drawdown_breaker("bot-1", 10_000.0)

        assert result is None, "breaker must fail open on DB error"
