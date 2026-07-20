"""
Regression tests for PipelineService.emit and the retry-failure telemetry hook.

Background: resilience.py and recovery/engine.py both called
`PipelineService.emit(...)` for a long time while no such method existed — the
only `emit` was a local function nested inside the cycle runner. Every call
raised AttributeError into a bare `except Exception: pass`, so no retry or
recovery event was ever recorded and nothing failed loudly enough to notice.

These tests pin the method's existence and the emitter's volume behaviour so
it cannot silently rot the same way again.
"""
import asyncio

import pytest
from unittest.mock import patch

from app.services.pipeline_service import PipelineService
from app.utils.resilience import _pipeline_emit, aresilient_call
import lazycat.resilience as sdk_resilience


@pytest.fixture
def captured():
    """Intercept DB writes and restore whatever cycle_id was in _state."""
    events = []
    prior = PipelineService._state.get("cycle_id")
    with patch.object(
        PipelineService,
        "_append_events_safe",
        staticmethod(lambda cycle_id, evs: events.append((cycle_id, evs))),
    ):
        yield events
    PipelineService._state["cycle_id"] = prior


# ── the method must exist ───────────────────────────────────────────────


def test_pipeline_service_has_a_callable_class_level_emit():
    # The bug this whole module exists for: attribute lookup used to fail.
    assert callable(getattr(PipelineService, "emit", None))


def test_resilience_emitter_is_registered_with_the_sdk():
    assert sdk_resilience._failure_emitter is _pipeline_emit


def test_recovery_engine_emit_path_writes_an_event(captured):
    PipelineService._state["cycle_id"] = "cyc_rec"
    PipelineService.emit(
        "recovery", "recovery_agent_step", "detail", status="warning", data={"a": 1}
    )
    assert len(captured) == 1
    cycle_id, events = captured[0]
    assert cycle_id == "cyc_rec"
    assert events[0]["phase"] == "recovery"
    assert events[0]["status"] == "warning"
    assert events[0]["data"] == {"a": 1}


# ── cycle scoping ───────────────────────────────────────────────────────


def test_emit_outside_a_cycle_logs_but_writes_nothing(captured):
    PipelineService._state["cycle_id"] = None
    PipelineService.emit("recovery", "step", "no cycle running")
    assert captured == []


def test_emit_does_not_hijack_cycle_progress(captured):
    # A background retry is ambient telemetry; showing it as the cycle's
    # current step would misreport what the pipeline is actually doing.
    PipelineService._state["cycle_id"] = "cyc_p"
    PipelineService._state["progress"] = "[ANALYSIS] real work"
    PipelineService.emit("recovery", "retry_x", "a retry happened")
    assert PipelineService._state["progress"] == "[ANALYSIS] real work"


# ── volume: one event per give-up, not per attempt ──────────────────────


@pytest.mark.asyncio
async def test_exhausted_retries_emit_exactly_one_event(captured):
    PipelineService._state["cycle_id"] = "cyc_v"

    @aresilient_call(retries=4, base_delay=0.001)
    async def always_fails():
        raise ConnectionError("blip")

    with pytest.raises(Exception):
        await always_fails()
    await asyncio.sleep(0.2)  # let the executor thread land

    assert len(captured) == 1, "expected only the give-up event, not one per attempt"
    event = captured[0][1][0]
    assert event["data"]["attempt"] == 4
    assert event["data"]["max_attempts"] == 4
    assert event["status"] == "error"


@pytest.mark.asyncio
async def test_successful_call_emits_nothing(captured):
    PipelineService._state["cycle_id"] = "cyc_ok"

    @aresilient_call(retries=3, base_delay=0.001)
    async def fine():
        return "ok"

    assert await fine() == "ok"
    await asyncio.sleep(0.05)
    assert captured == []


@pytest.mark.asyncio
async def test_call_that_recovers_emits_nothing(captured):
    # Interim failures are already in the logs; only a give-up is actionable.
    PipelineService._state["cycle_id"] = "cyc_r"
    calls = []

    @aresilient_call(retries=3, base_delay=0.001)
    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("blip")
        return "recovered"

    assert await flaky() == "recovered"
    await asyncio.sleep(0.05)
    assert captured == []


def test_every_attempt_mode_emits_each_failure(captured):
    # The debugging escape hatch (RESILIENCE_EMIT_EVERY_ATTEMPT=true).
    PipelineService._state["cycle_id"] = "cyc_all"
    with patch("app.utils.resilience._EMIT_EVERY_ATTEMPT", True):
        _pipeline_emit("f", 1, 3, sdk_resilience.FailureType.TRANSIENT, ValueError("x"), 5)
        _pipeline_emit("f", 2, 3, sdk_resilience.FailureType.TRANSIENT, ValueError("x"), 5)
    assert len(captured) == 2
    assert captured[0][1][0]["status"] == "warning"  # not the final attempt


# ── telemetry must never break the caller ───────────────────────────────


def test_append_failure_is_swallowed():
    PipelineService._state["cycle_id"] = "cyc_boom"

    def explode(cycle_id, events):
        raise RuntimeError("db down")

    with patch(
        "app.services.pipeline_service.PipelineStateDB.append_events", explode
    ):
        # _append_events_safe must contain it; emit must not propagate.
        PipelineService._append_events_safe("cyc_boom", [{"phase": "p"}])
        PipelineService.emit("recovery", "s", "d")
