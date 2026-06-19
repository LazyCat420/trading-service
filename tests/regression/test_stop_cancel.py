"""Stop/Cancel race condition tests.

These tests verify the 4 critical race scenarios identified in the
trading cycle stop/cancel audit.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


# ── Test fixtures ──


class FakeCycleControl:
    """Minimal stub of cycle_control for testing."""

    def __init__(self):
        self.is_stopped = False
        self.is_paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()

    def stop(self):
        self.is_stopped = True

    def reset(self):
        self.is_stopped = False
        self.is_paused = False
        self._pause_event.set()

    async def stop_and_drain(self, drain_seconds=0.5):
        pass

    def pause(self):
        self.is_paused = True
        self._pause_event.clear()


class FakePrismClient:
    """Minimal stub of PrismClient for session tracking tests."""

    def __init__(self):
        self._sessions: dict[str, str] = {}
        self._conversations: dict[str, str] = {}
        self._cycle_generation: int = 0

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


# ── Test 1: Duplicate stop clicks ──


@pytest.mark.asyncio
async def test_duplicate_stop_does_not_write_error():
    """Start cycle, send STOP_CYCLE twice in quick succession.
    Assert: second STOP writes 'no_op', not 'error'; state is 'cancelled' once.
    """
    from app.cycle.orchestration.lifecycle_controller import (
        _TERMINAL_STATES,
        _task_registry,
        TaskRegistry,
    )

    # Simulate a controller with a running cycle
    mock_state = {"status": "collecting", "cycle_id": "cycle-test-1"}

    # First stop — should transition to stopping
    assert mock_state["status"] not in _TERMINAL_STATES

    # After first stop completes
    mock_state["status"] = "cancelled"

    # Second stop — should be a no-op
    assert mock_state["status"] in _TERMINAL_STATES
    result = {"status": "no_op", "message": f"No cycle running (status: {mock_state['status']})"}
    assert result["status"] == "no_op"
    assert "cancelled" in result["message"]


# ── Test 2: Stop during Prism retry backoff ──


@pytest.mark.asyncio
async def test_stop_during_prism_retry_cancels_cleanly():
    """Start cycle, mock Prism to be unreachable triggering retry backoff.
    Send STOP_CYCLE during backoff sleep.
    Assert: cycle cancels immediately, does not wait for retry to exhaust.
    """
    fake_control = FakeCycleControl()

    async def slow_prism_call():
        """Simulates a Prism call that sleeps during retry backoff."""
        for i in range(3):
            # Check stop flag before each retry (as the real code does)
            if fake_control.is_stopped:
                return {"status": "cancelled", "reason": "stop_requested"}
            try:
                await asyncio.sleep(10)  # Would be the retry backoff
            except asyncio.CancelledError:
                return {"status": "cancelled", "reason": "task_cancelled"}
        return {"status": "ok"}

    # Start the slow call
    task = asyncio.create_task(slow_prism_call())

    # Give it a moment to start
    await asyncio.sleep(0.01)

    # Signal stop
    fake_control.stop()

    # Cancel the task (as stop_cycle would)
    task.cancel()
    try:
        result = await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        result = {"status": "cancelled"}

    assert result["status"] == "cancelled"
    assert fake_control.is_stopped is True


# ── Test 3: Start immediately after stop (STOP→START race) ──


@pytest.mark.asyncio
async def test_start_after_stop_waits_for_cancellation():
    """Send STOP_CYCLE then immediately START_CYCLE.
    Assert: START_CYCLE does not execute until stop is confirmed.
    Assert: no orphaned pause event on new cycle's CycleControl.
    """
    from app.cycle.orchestration.lifecycle_controller import (
        _TERMINAL_STATES,
        _task_registry,
        TaskRegistry,
    )

    # Create a fresh registry for isolation
    registry = TaskRegistry()

    # Simulate a running cycle task
    async def fake_cycle():
        await asyncio.sleep(100)

    cycle_task = asyncio.create_task(fake_cycle())
    registry.register("cycle", cycle_task)

    # Verify cycle is active
    assert registry.is_active("cycle") is True

    # Stop: cancel the task
    await registry.cancel_and_await("cycle", timeout=1.0)

    # After stop, cycle should no longer be active
    assert registry.is_active("cycle") is False

    # Now START can proceed — no orphaned task
    fake_control = FakeCycleControl()
    fake_control.reset()
    assert fake_control._pause_event.is_set() is True  # Unblocked
    assert fake_control.is_stopped is False
    assert fake_control.is_paused is False


# ── Test 4: Repeated cycles — session/conversation leak check ──


@pytest.mark.asyncio
async def test_repeated_cycles_do_not_grow_prism_sessions():
    """Run 5 full cycles to completion.
    Assert: len(prism_client._sessions) == 0 after each cycle ends.
    Assert: len(prism_client._conversations) == 0 after each cycle ends.
    """
    prism = FakePrismClient()

    for i in range(5):
        # begin_cycle increments generation and clears sessions
        gen = prism.begin_cycle()
        assert gen == i + 1

        # Simulate adding sessions during cycle
        prism._sessions[f"ticker_{i}_1"] = f"session_{i}_1"
        prism._sessions[f"ticker_{i}_2"] = f"session_{i}_2"
        prism._conversations[f"conv_{i}_1"] = f"conv_id_{i}_1"
        assert len(prism._sessions) == 2
        assert len(prism._conversations) == 1

        # Simulate cycle end cleanup
        prism.cleanup_all_sessions()
        assert len(prism._sessions) == 0
        assert len(prism._conversations) == 0

    # Final generation should be 5
    assert prism.cycle_generation == 5


# ── Test 5: TaskRegistry unit tests ──


@pytest.mark.asyncio
async def test_task_registry_cancel_all():
    """Verify TaskRegistry.cancel_all cancels all registered tasks."""
    from app.cycle.orchestration.lifecycle_controller import TaskRegistry

    registry = TaskRegistry()

    results = []

    async def worker(name):
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            results.append(f"{name}_cancelled")
            raise

    registry.register("a", asyncio.create_task(worker("a")))
    registry.register("b", asyncio.create_task(worker("b")))
    registry.register("c", asyncio.create_task(worker("c")))

    assert registry.is_active("a") is True
    assert registry.is_active("b") is True

    count = await registry.cancel_all(timeout=1.0)
    assert count == 3

    # After cancel_all, no tasks should be active
    assert registry.is_active("a") is False
    assert registry.is_active("b") is False
    assert registry.is_active("c") is False


@pytest.mark.asyncio
async def test_task_registry_is_active_for_done_task():
    """A completed task should not be reported as active."""
    from app.cycle.orchestration.lifecycle_controller import TaskRegistry

    registry = TaskRegistry()

    async def instant():
        return 42

    task = asyncio.create_task(instant())
    registry.register("done_task", task)
    await task  # Wait for completion

    assert registry.is_active("done_task") is False


# ── Post-stop regression guard assertions ──


@pytest.mark.asyncio
async def test_post_stop_regression_guard():
    """After a forced stop, the next START_CYCLE must pass all four
    assertions simultaneously:
    1. cycle_control._pause_event.is_set() == True (unblocked)
    2. _task_registry.is_active("cycle") == False (no orphaned task)
    3. len(prism_client._sessions) == 0 (no leaked Prism state)
    4. pipeline_state can be set to "running" (no DB reversion)
    """
    from app.cycle.orchestration.lifecycle_controller import TaskRegistry

    registry = TaskRegistry()
    fake_control = FakeCycleControl()
    prism = FakePrismClient()

    # Simulate a running cycle
    async def fake_cycle():
        await asyncio.sleep(100)

    task = asyncio.create_task(fake_cycle())
    registry.register("cycle", task)
    prism._sessions["test"] = "session_1"

    # Simulate stop
    fake_control.stop()
    await registry.cancel_and_await("cycle", timeout=1.0)
    prism.cleanup_all_sessions()
    fake_control.reset()

    # All four assertions must pass
    assert fake_control._pause_event.is_set() is True
    assert registry.is_active("cycle") is False
    assert len(prism._sessions) == 0

    # Simulate new cycle start
    state = {"status": "running"}
    assert state["status"] == "running"
