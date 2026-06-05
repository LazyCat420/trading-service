"""
Test: Trading Phase Execution — Verify trades actually execute.

Covers the critical regression where `await _get_current_price(ticker)` crashed
because _get_current_price is a sync function returning a tuple, not a coroutine.
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.fixture
def mock_portfolio():
    """Standard portfolio state for testing."""
    return {
        "bot_id": "test-bot",
        "cash": 50000.0,
        "total_pnl": 0.0,
        "positions": [
            {"ticker": "HIMS", "qty": 100, "avg_entry_price": 25.0, "stop_loss_pct": 0.08, "opened_at": "2026-01-01"},
        ],
        "position_count": 1,
    }


@pytest.fixture
def sample_decisions():
    """Mix of BUY/SELL/HOLD decisions from analysis phase."""
    return [
        {"ticker": "AAPL", "action": "BUY", "confidence": 85, "rationale": "Strong momentum"},
        {"ticker": "NVDA", "action": "BUY", "confidence": 72, "rationale": "AI growth"},
        {"ticker": "HIMS", "action": "SELL", "confidence": 90, "rationale": "Deteriorating fundamentals"},
        {"ticker": "MSFT", "action": "HOLD", "confidence": 60, "rationale": "Neutral outlook"},
        {"ticker": "TSLA", "action": "BUY", "confidence": 50, "rationale": "Low confidence"},  # Below 70 threshold
        {"ticker": "BAD", "action": "BUY", "confidence": 80, "rationale": "Test", "is_timeout_fallback": True},
    ]


class TestGetCurrentPriceNotAwaited:
    """Regression: _get_current_price is sync and returns a tuple — must NOT be awaited."""

    def test_get_current_price_is_sync(self):
        """Verify _get_current_price is not a coroutine function."""
        from app.trading.paper_trader import _get_current_price
        assert not asyncio.iscoroutinefunction(_get_current_price), (
            "_get_current_price must be a sync function, not async. "
            "If this changes, trading_phase.py must be updated to await it."
        )

    def test_get_current_price_returns_tuple(self):
        """Verify _get_current_price returns a tuple, not a single value."""
        from app.trading.paper_trader import _get_current_price
        import inspect
        sig = inspect.signature(_get_current_price)
        # Check the return annotation
        ret = sig.return_annotation
        assert "tuple" in str(ret).lower(), (
            f"_get_current_price return type should be tuple, got: {ret}"
        )


class TestTradingPhaseExecution:
    """Integration tests for execute_decisions."""

    @pytest.mark.asyncio
    async def test_buy_executes_with_valid_price(self, mock_portfolio, sample_decisions):
        """BUY decisions with valid price data should execute."""
        buy_only = [d for d in sample_decisions if d["action"] == "BUY" and not d.get("is_timeout_fallback") and d["confidence"] >= 70]

        with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
             patch("app.cycle.trading_phase._get_current_price", return_value=(150.0, 2.0)), \
             patch("app.cycle.trading_phase.buy", new_callable=AsyncMock, return_value={"action": "BUY", "ticker": "AAPL", "qty": 10, "price": 150.0, "amount": 1500.0}), \
             patch("app.cycle.trading_phase.sell", new_callable=AsyncMock), \
             patch("app.cycle.trading_phase.check_portfolio_constraints", return_value=(True, "BUY allowed")), \
             patch("app.cycle.trading_phase.record_trade"), \
             patch("app.cycle.trading_phase.cycle_control") as mock_cc, \
             patch("app.services.pipeline_service.PipelineService") as mock_ps:

            mock_cc.wait_if_paused = AsyncMock()

            from app.cycle.trading_phase import execute_decisions
            result = await execute_decisions(buy_only, bot_id="test-bot", cycle_id="test-cycle")

            assert result is not None
            counts = result["counts"]
            # At least one BUY should have executed
            assert counts["buy_executed"] > 0, (
                f"Expected at least 1 buy_executed but got {counts}. "
                f"Skipped: {result.get('skipped', [])}"
            )

    @pytest.mark.asyncio
    async def test_sell_executes_for_held_position(self, mock_portfolio):
        """SELL decisions for held positions should execute."""
        sell_decisions = [
            {"ticker": "HIMS", "action": "SELL", "confidence": 90, "rationale": "Exit signal"},
        ]

        with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
             patch("app.cycle.trading_phase._get_current_price", return_value=(30.0, 1.0)), \
             patch("app.cycle.trading_phase.buy", new_callable=AsyncMock), \
             patch("app.cycle.trading_phase.sell", new_callable=AsyncMock, return_value={"action": "SELL", "ticker": "HIMS", "qty": 100, "price": 30.0, "proceeds": 3000.0, "realized_pnl": 500.0, "pnl_pct": 20.0}), \
             patch("app.cycle.trading_phase.check_portfolio_constraints", return_value=(True, "SELL allowed")), \
             patch("app.cycle.trading_phase.resolve_outcome", new_callable=AsyncMock), \
             patch("app.cycle.trading_phase.record_trade"), \
             patch("app.cycle.trading_phase.cycle_control") as mock_cc, \
             patch("app.services.pipeline_service.PipelineService"):

            mock_cc.wait_if_paused = AsyncMock()

            from app.cycle.trading_phase import execute_decisions
            result = await execute_decisions(sell_decisions, bot_id="test-bot", cycle_id="test-cycle")

            counts = result["counts"]
            assert counts["sell_executed"] == 1, f"Expected 1 sell_executed but got {counts}"

    @pytest.mark.asyncio
    async def test_crash_fallbacks_are_skipped(self, mock_portfolio, sample_decisions):
        """Crash fallback results should be filtered before trade execution."""
        with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
             patch("app.cycle.trading_phase._get_current_price", return_value=(100.0, 1.0)), \
             patch("app.cycle.trading_phase.buy", new_callable=AsyncMock), \
             patch("app.cycle.trading_phase.sell", new_callable=AsyncMock), \
             patch("app.cycle.trading_phase.check_portfolio_constraints", return_value=(True, "BUY allowed")), \
             patch("app.cycle.trading_phase.record_trade"), \
             patch("app.cycle.trading_phase.cycle_control") as mock_cc, \
             patch("app.services.pipeline_service.PipelineService"):

            mock_cc.wait_if_paused = AsyncMock()

            from app.cycle.trading_phase import execute_decisions
            result = await execute_decisions(sample_decisions, bot_id="test-bot", cycle_id="test-cycle")

            counts = result["counts"]
            assert counts["crash_fallbacks"] == 1, f"Expected 1 crash_fallback but got {counts}"

    @pytest.mark.asyncio
    async def test_holds_are_counted_not_traded(self, mock_portfolio, sample_decisions):
        """HOLD decisions should be counted but not dispatched to executor."""
        with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
             patch("app.cycle.trading_phase._get_current_price", return_value=(100.0, 1.0)), \
             patch("app.cycle.trading_phase.buy", new_callable=AsyncMock, return_value={"action": "BUY", "ticker": "test", "qty": 1, "price": 100, "amount": 100}), \
             patch("app.cycle.trading_phase.sell", new_callable=AsyncMock, return_value={"action": "SELL", "ticker": "HIMS", "qty": 100, "price": 30, "proceeds": 3000, "realized_pnl": 500, "pnl_pct": 20}), \
             patch("app.cycle.trading_phase.check_portfolio_constraints", return_value=(True, "allowed")), \
             patch("app.cycle.trading_phase.resolve_outcome", new_callable=AsyncMock), \
             patch("app.cycle.trading_phase.record_trade"), \
             patch("app.cycle.trading_phase.cycle_control") as mock_cc, \
             patch("app.services.pipeline_service.PipelineService"):

            mock_cc.wait_if_paused = AsyncMock()

            from app.cycle.trading_phase import execute_decisions
            result = await execute_decisions(sample_decisions, bot_id="test-bot", cycle_id="test-cycle")

            counts = result["counts"]
            assert counts["holds"] >= 1, f"Expected at least 1 hold but got {counts}"

    @pytest.mark.asyncio
    async def test_no_price_data_skips_buy(self, mock_portfolio):
        """BUY should be skipped when no price data is available."""
        decisions = [{"ticker": "FAKE", "action": "BUY", "confidence": 85, "rationale": "test"}]

        with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
             patch("app.cycle.trading_phase._get_current_price", return_value=(None, None)), \
             patch("app.cycle.trading_phase.buy", new_callable=AsyncMock), \
             patch("app.cycle.trading_phase.check_portfolio_constraints", return_value=(True, "BUY allowed")), \
             patch("app.cycle.trading_phase.cycle_control") as mock_cc, \
             patch("app.services.pipeline_service.PipelineService"):

            mock_cc.wait_if_paused = AsyncMock()

            from app.cycle.trading_phase import execute_decisions
            result = await execute_decisions(decisions, bot_id="test-bot", cycle_id="test-cycle")

            counts = result["counts"]
            assert counts["buy_failed"] == 1
            assert counts["buy_executed"] == 0

    @pytest.mark.asyncio
    async def test_low_confidence_buy_gets_zero_size(self, mock_portfolio):
        """BUY with confidence < 70 should get size_pct=0 from position sizer and be blocked."""
        decisions = [{"ticker": "TSLA", "action": "BUY", "confidence": 50, "rationale": "Low confidence"}]

        with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
             patch("app.cycle.trading_phase._get_current_price", return_value=(200.0, 1.0)), \
             patch("app.cycle.trading_phase.buy", new_callable=AsyncMock), \
             patch("app.cycle.trading_phase.check_portfolio_constraints", return_value=(True, "BUY allowed")), \
             patch("app.cycle.trading_phase.cycle_control") as mock_cc, \
             patch("app.services.pipeline_service.PipelineService"):

            mock_cc.wait_if_paused = AsyncMock()

            from app.cycle.trading_phase import execute_decisions
            result = await execute_decisions(decisions, bot_id="test-bot", cycle_id="test-cycle")

            counts = result["counts"]
            # confidence=50 < MIN_CONF=70 → size_pct=0 → amount=0 → blocked
            assert counts["buy_executed"] == 0, f"Low confidence BUY should not execute: {counts}"
            assert counts["blocked"] == 1, f"Should be blocked due to zero size: {counts}"

    @pytest.mark.asyncio
    async def test_one_ticker_failure_doesnt_abort_others(self, mock_portfolio):
        """Per-ticker error handling: one failing ticker should not prevent others from trading."""
        decisions = [
            {"ticker": "GOOD1", "action": "BUY", "confidence": 85, "rationale": "test"},
            {"ticker": "GOOD2", "action": "BUY", "confidence": 80, "rationale": "test"},
        ]

        call_count = 0

        def mock_price(ticker):
            nonlocal call_count
            call_count += 1
            if ticker == "GOOD1":
                raise ValueError("Simulated price lookup crash")
            return (150.0, 1.0)

        with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
             patch("app.cycle.trading_phase._get_current_price", side_effect=mock_price), \
             patch("app.cycle.trading_phase.buy", new_callable=AsyncMock, return_value={"action": "BUY", "ticker": "GOOD2", "qty": 5, "price": 150.0, "amount": 750.0}), \
             patch("app.cycle.trading_phase.check_portfolio_constraints", return_value=(True, "BUY allowed")), \
             patch("app.cycle.trading_phase.record_trade"), \
             patch("app.cycle.trading_phase.cycle_control") as mock_cc, \
             patch("app.services.pipeline_service.PipelineService"):

            mock_cc.wait_if_paused = AsyncMock()

            from app.cycle.trading_phase import execute_decisions
            result = await execute_decisions(decisions, bot_id="test-bot", cycle_id="test-cycle")

            counts = result["counts"]
            # GOOD1 should fail, GOOD2 should succeed
            assert counts["buy_executed"] >= 1, (
                f"GOOD2 should have executed despite GOOD1 failure. Counts: {counts}, "
                f"Skipped: {result.get('skipped', [])}"
            )
            assert counts["buy_failed"] >= 1, "GOOD1 should have been recorded as failed"
