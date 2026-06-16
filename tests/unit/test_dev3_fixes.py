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
