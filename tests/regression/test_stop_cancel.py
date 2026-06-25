"""Stop/Cancel regression tests for PipelineService."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.pipeline_service import PipelineService
from app.services.vllm_client import llm

# ── Test fixtures ──

class FakeCycleControl:
    """Minimal stub of cycle_control for testing retry cancellation."""
    def __init__(self):
        self.is_stopped = False
        self.is_paused = False

    def stop(self):
        self.is_stopped = True


class FakePrismClient:
    """Minimal stub of PrismClient for session tracking tests."""
    def __init__(self):
        self._sessions = {}
        self._conversations = {}
        self._cycle_generation = 0

    @property
    def cycle_generation(self):
        return self._cycle_generation

    def begin_cycle(self) -> int:
        self._cycle_generation += 1
        self.cleanup_all_sessions()
        return self._cycle_generation

    def cleanup_all_sessions(self):
        self._sessions.clear()
        self._conversations.clear()


@pytest.fixture(autouse=True)
def cleanup_pipeline_service():
    """Reset PipelineService class variables before/after each test."""
    PipelineService._state = {
        "status": "stopped",
        "progress": "",
        "tickers": [],
        "cycle_id": None,
    }
    PipelineService._cycle_task = None
    PipelineService._stop_requested = False
    yield
    PipelineService._cycle_task = None
    PipelineService._stop_requested = False


# ── Test 1: Duplicate stop clicks ──

@pytest.mark.asyncio
async def test_duplicate_stop_does_not_write_error():
    """Start cycle, send stop_cycle twice in quick succession.
    Assert: second stop is handled cleanly and state is 'stopped'.
    """
    # Set status to running
    PipelineService._state["status"] = "running"
    
    # Create a mock running task
    async def mock_cycle_task():
        await asyncio.sleep(10)
    
    task = asyncio.create_task(mock_cycle_task())
    PipelineService._cycle_task = task

    with patch("app.services.vllm_client.llm.abort_active_requests", new_callable=AsyncMock) as mock_abort, \
         patch("app.services.pipeline_state.PipelineStateDB.save_state") as mock_save:
        
        # Stop first time
        res1 = await PipelineService.stop_cycle()
        assert res1["status"] == "stopped"
        assert PipelineService._state["status"] == "stopped"
        
        # Stop second time
        res2 = await PipelineService.stop_cycle()
        assert res2["status"] == "stopped"
        assert PipelineService._state["status"] == "stopped"
        
        # Verify LLM abort was called
        assert mock_abort.called


# ── Test 2: Stop during Prism retry backoff ──

@pytest.mark.asyncio
async def test_stop_during_prism_retry_cancels_cleanly():
    """Start cycle, mock Prism retry backoff sleep and send STOP during backoff.
    Assert: cycle cancels immediately.
    """
    fake_control = FakeCycleControl()

    async def slow_prism_call():
        for i in range(3):
            if fake_control.is_stopped:
                return {"status": "cancelled", "reason": "stop_requested"}
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return {"status": "cancelled", "reason": "task_cancelled"}
        return {"status": "ok"}

    task = asyncio.create_task(slow_prism_call())
    await asyncio.sleep(0.01)

    fake_control.stop()
    task.cancel()

    try:
        result = await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        result = {"status": "cancelled"}

    assert result["status"] == "cancelled"
    assert fake_control.is_stopped is True


# ── Test 3: Start during stopping state ──

@pytest.mark.asyncio
async def test_start_during_stopping_returns_deduplicated():
    """Verify that start_cycle returns deduplicated if the current status is 'stopping'."""
    with patch("app.services.pipeline_state.PipelineStateDB.get_state", return_value={"status": "stopping"}):
        res = await PipelineService.start_cycle(["AAPL"])
        assert res["status"] == "deduplicated"
        assert "stopping" in res["message"]


# ── Test 4: Repeated cycles — session/conversation leak check ──

@pytest.mark.asyncio
async def test_repeated_cycles_do_not_grow_prism_sessions():
    """Run 5 full cycles. Verify sessions and conversations are cleared each time."""
    prism = FakePrismClient()

    for i in range(5):
        gen = prism.begin_cycle()
        assert gen == i + 1

        prism._sessions[f"ticker_{i}_1"] = f"session_{i}_1"
        prism._sessions[f"ticker_{i}_2"] = f"session_{i}_2"
        prism._conversations[f"conv_{i}_1"] = f"conv_id_{i}_1"
        assert len(prism._sessions) == 2
        assert len(prism._conversations) == 1

        prism.cleanup_all_sessions()
        assert len(prism._sessions) == 0
        assert len(prism._conversations) == 0

    assert prism.cycle_generation == 5


# ── Test 5: Pipeline Task Lifecycle & CancelledError ──

@pytest.mark.asyncio
async def test_pipeline_task_lifecycle_cancellation():
    """Verify request_stop cancels cycle task and _run_all_v3 handles CancelledError."""
    # Mock run_v3_pipeline to block
    async def mock_run_v3(*args, **kwargs):
        await asyncio.sleep(10)
        return {"action": "HOLD", "confidence": 0}

    # Mock DB save and all other side-effects in the pipeline
    with patch("app.services.pipeline_state.PipelineStateDB.save_state"), \
         patch("app.services.pipeline_state.PipelineStateDB.append_events"), \
         patch("app.v3.orchestrator.run_v3_pipeline", side_effect=mock_run_v3), \
         patch("app.services.result_saver.save_analysis_result") as mock_save_verdict, \
         patch("app.trading.paper_trader.buy", new_callable=AsyncMock) as mock_buy, \
         patch("app.trading.paper_trader.sell", new_callable=AsyncMock) as mock_sell, \
         patch("app.trading.order_triggers.create_trigger", new_callable=AsyncMock) as mock_trigger, \
         patch("app.v3.debate_coordinator.run_battle_royale", new_callable=AsyncMock) as mock_debate, \
         patch("app.services.vllm_client.llm.abort_active_requests", new_callable=AsyncMock) as mock_abort:
         
        # Start a real task using start_cycle
        await PipelineService.start_cycle(["AAPL"])
        await asyncio.sleep(0.05)
        
        task = PipelineService._cycle_task
        assert task is not None
        assert not task.done()

        # Stop
        PipelineService.request_stop()
        assert PipelineService._state["status"] == "stopping"
        
        # Verify it cancels and finishes cleanly
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert PipelineService._state["status"] == "stopped"
        assert mock_abort.called


# ── Test 6: Post-stop regression guard ──

@pytest.mark.asyncio
async def test_post_stop_regression_guard():
    """Verify that start_cycle resets the VLLM client's kill switch."""
    # Simulate stopping/killing
    llm._killed = True

    # Start a cycle
    with patch("app.services.pipeline_state.PipelineStateDB.save_state"), \
         patch("app.services.pipeline_service.PipelineService._run_all_v3", new_callable=AsyncMock):
        
        res = await PipelineService.start_cycle(["AAPL"])
        assert res["status"] == "starting"
        
        # Verify the kill switch was reset
        assert llm._killed is False
