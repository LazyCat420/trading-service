import asyncio
import pytest
from app.v3.orchestrator import _run_agent_with_circuit_breaker
from app.v3.shared_desk import SharedDesk, DeskPhase
from app.v3.guardrails import CircuitBreaker

@pytest.mark.asyncio
async def test_regime_engine_failure_mode():
    desk = SharedDesk("META")
    breaker = CircuitBreaker()
    
    # Mock agent_module
    class MockRegimeEngine:
        AGENT_NAME = "v3_regime_engine"
        TOOL_WHITELIST = []
        SYSTEM_PROMPT = "Mock prompt"
        ARTIFACT_TYPE = "regime_classification"
    
    # Mock emit
    def mock_emit(*args, **kwargs):
        pass
        
    # Simulate run_agent failing (returning None due to context limit)
    # Actually _run_agent_with_circuit_breaker wraps run_agent in a thread pool.
    # To mock it effectively, we might need to mock agent_runner.py's run_agent.
    # Let's just patch it.
    
    import app.v3.agent_runner
    original_run_agent = app.v3.agent_runner.run_v3_agent
    
    def mocked_run_agent(*args, **kwargs):
        return None  # Simulate failure
        
    app.v3.agent_runner.run_v3_agent = mocked_run_agent
    
    try:
        outcome = await _run_agent_with_circuit_breaker(
            desk=desk,
            agent_module=MockRegimeEngine,
            phase_name="regime_engine",
            breaker=breaker,
            cycle_id="test_cycle",
            bot_id="bot123",
            emit=mock_emit
        )
        
        print(f"Outcome: {outcome}")
        print(f"Desk Phase after failure: {desk.phase}")
        
        # Now simulate orchestrator's wrap-up
        try:
            desk.advance_phase(DeskPhase.PM_DONE)
            print("Successfully transitioned to PM_DONE (THIS SHOULD NOT HAPPEN)")
        except Exception as e:
            print(f"Orchestrator crash reproduced! Exception: {e}")
            
    finally:
        app.v3.agent_runner.run_v3_agent = original_run_agent

if __name__ == "__main__":
    asyncio.run(test_regime_engine_failure_mode())
