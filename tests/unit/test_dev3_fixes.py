import pytest
from app.recovery.failure_types import FailureEvent, FailureType, RecoveryAction
from app.recovery.engine import recovery_engine

def test_coral_is_dormant():
    """Verify that CORAL is dormant and forces SKIP on non-transient failures."""
    recovery_engine.reset_cycle("test_cycle")
    
    event = FailureEvent(
        failure_type=FailureType.DEGRADED,
        agent_name="test_agent",
        step_name="test_step",
        ticker="AAPL",
        exception_type="ValueError",
        exception_msg="Test error",
        attempt=1,
        max_attempts=3,
        timestamp=1672531200.0
    )
    
    result = recovery_engine.handle(event)
    
    # Since CORAL is dormant, DEGRADED should return SKIP, not RETRY_DEGRADED or REPAIR
    assert result.action == RecoveryAction.SKIP
    assert "CORAL Dormant" in result.reason

def test_coral_allows_transient():
    """Verify that CORAL still allows TRANSIENT to retry."""
    recovery_engine.reset_cycle("test_cycle_2")
    
    event = FailureEvent(
        failure_type=FailureType.TRANSIENT,
        agent_name="test_agent",
        step_name="test_step",
        ticker="AAPL",
        exception_type="TimeoutError",
        exception_msg="Timeout",
        attempt=1,
        max_attempts=3,
        timestamp=1672531200.0
    )
    
    result = recovery_engine.handle(event)
    assert result.action == RecoveryAction.RETRY

@pytest.mark.asyncio
async def test_analysis_task_timeout():
    """Verify that a timeout during analysis worker execution cancels the task but allows the orchestrator to continue gracefully."""
    import asyncio
    from unittest.mock import patch
    from app.services.pipeline_service import PipelineService
    from app.cycle.context import CycleContext
    
    ctx = CycleContext(
        cycle_id="test_timeout_cycle",
        tickers=["AAPL"],
        collect=True,
        analyze=True,
        trade=False
    )
    
    with patch("app.cycle.orchestration.orchestrator_core.run_phase1_health") as mock_health, \
         patch("app.cycle.orchestration.orchestrator_core.run_phase2_collection", return_value=["AAPL"]) as mock_collect, \
         patch("app.cycle.orchestration.orchestrator_core.run_phase4_analysis") as mock_analysis, \
         patch("app.cycle.orchestration.orchestrator_core.run_phase6_post") as mock_post, \
         patch("app.cycle.orchestration.orchestrator_core.asyncio.wait_for", side_effect=asyncio.TimeoutError) as mock_wait_for, \
         patch.object(PipelineService, "emit") as mock_emit, \
         patch.object(PipelineService, "save_state") as mock_save:
         
        PipelineService._state = {"results": []}
        import time
        PipelineService._start_time = time.monotonic()
        
        await PipelineService._execute_cycle_impl(ctx, "test_bot")
        
        mock_wait_for.assert_called()
        assert PipelineService._state["status"] == "done"

@pytest.mark.asyncio
async def test_portfolio_gate_integration_in_trading_phase():
    """Verify that execute_decisions integrates and enforces the actual check_portfolio_gate decisions."""
    from unittest.mock import patch
    from app.cycle.trading_phase import execute_decisions
    
    decisions = [
        {"ticker": "MSFT", "action": "BUY", "confidence": 95, "rationale": "High conviction buy"}
    ]
    
    mock_portfolio = {
        "cash": 5000.0,
        "positions": []
    }
    
    with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
         patch("app.cycle.trading_phase.check_portfolio_gate", return_value={"blocked": True, "reason": "Constitutional Concentration Limit Exceeded"}) as mock_gate, \
         patch("app.cycle.trading_phase.buy") as mock_buy:
         
        res = await execute_decisions(decisions, bot_id="test-bot", cycle_id="test-cycle")
        
        mock_gate.assert_called_once_with("MSFT", "BUY", "test-bot", 95)
        mock_buy.assert_not_called()
        assert res["counts"]["blocked"] == 1
        assert len(res["skipped"]) == 1
        assert res["skipped"][0]["ticker"] == "MSFT"
        assert res["skipped"][0]["reason"] == "Constitutional Concentration Limit Exceeded"

