import pytest
import asyncio
from datetime import datetime, timezone
from app.telemetry.schema import TelemetryEvent
from app.telemetry.state import CycleState
from app.telemetry import bus

def test_telemetry_event_serialization():
    event = TelemetryEvent(
        ts="2026-06-08T12:00:00Z",
        cycle_id="test-cycle-123",
        ticker="AAPL",
        phase="analyzing",
        kind="llm",
        source="prism",
        status="ok",
        step="prism_agent_start",
        detail="Testing event",
        elapsed_ms=100,
        data={"some_key": "some_value"}
    )
    d = event.to_dict()
    assert d["cycle_id"] == "test-cycle-123"
    assert d["ticker"] == "AAPL"
    assert d["data"] == {"some_key": "some_value"}

def test_cycle_state_serialization():
    state = CycleState(
        cycle_id="test-cycle-123",
        status="running",
        phase="analyzing",
        tickers=["AAPL", "MSFT"]
    )
    d = state.to_dict()
    assert d["cycle_id"] == "test-cycle-123"
    assert d["status"] == "running"
    assert d["tickers"] == ["AAPL", "MSFT"]

def test_telemetry_bus_publish_and_get():
    cycle_id = "test-cycle-publish"
    
    # Initialize state
    state = bus.get_cycle_state(cycle_id)
    assert state.cycle_id == cycle_id
    assert state.status == "idle"

    # Publish start event
    event1 = TelemetryEvent(
        ts="2026-06-08T12:00:00Z",
        cycle_id=cycle_id,
        ticker="",
        phase="init",
        kind="pipeline",
        source="cycle_runner",
        status="ok",
        step="init",
        detail="Cycle initialized",
        data={"tickers": ["AAPL", "NVDA"]}
    )
    bus.publish_event(event1)

    state = bus.get_cycle_state(cycle_id)
    assert state.tickers == ["AAPL", "NVDA"]
    assert state.status == "running"
    assert state.phase == "init"
    assert len(state.events) == 1
    assert state.started_at == "2026-06-08T12:00:00Z"

    # Publish result event
    event2 = TelemetryEvent(
        ts="2026-06-08T12:05:00Z",
        cycle_id=cycle_id,
        ticker="AAPL",
        kind="pipeline",
        source="cycle_runner",
        status="ok",
        step="result",
        detail="AAPL analyzed",
        data={"result": {"ticker": "AAPL", "decision": "BUY"}}
    )
    bus.publish_event(event2)

    state = bus.get_cycle_state(cycle_id)
    assert len(state.results) == 1
    assert state.results[0] == {"ticker": "AAPL", "decision": "BUY"}

@pytest.mark.asyncio
async def test_telemetry_bus_subscription():
    cycle_id = "test-cycle-sub"
    q = bus.subscribe()
    
    event = TelemetryEvent(
        ts="2026-06-08T12:10:00Z",
        cycle_id=cycle_id,
        ticker="",
        kind="pipeline",
        source="cycle_runner",
        status="ok",
        step="test_step",
        detail="Subscription test"
    )
    bus.publish_event(event)

    # Dequeue event from subscriber queue
    received = await asyncio.wait_for(q.get(), timeout=2.0)
    assert received["cycle_id"] == cycle_id
    assert received["step"] == "test_step"

    bus.unsubscribe(q)

def test_telemetry_bus_phases():
    cycle_id = "test-cycle-phases"
    
    # 1. Pause Phase
    bus.publish_event(TelemetryEvent(
        ts="2026-06-08T12:20:00Z",
        cycle_id=cycle_id,
        ticker="",
        phase="paused",
        kind="pipeline",
        source="cycle_runner",
        status="ok",
        step="user_pause",
        detail="Paused"
    ))
    state = bus.get_cycle_state(cycle_id)
    assert state.status == "paused"

    # 2. Resume Phase
    bus.publish_event(TelemetryEvent(
        ts="2026-06-08T12:21:00Z",
        cycle_id=cycle_id,
        ticker="",
        phase="resumed",
        kind="pipeline",
        source="cycle_runner",
        status="ok",
        step="user_resume",
        detail="Resumed"
    ))
    state = bus.get_cycle_state(cycle_id)
    assert state.status == "running"

    # 3. Interrupted Phase
    bus.publish_event(TelemetryEvent(
        ts="2026-06-08T12:22:00Z",
        cycle_id=cycle_id,
        ticker="",
        phase="interrupted",
        kind="pipeline",
        source="cycle_runner",
        status="ok",
        step="user_stop",
        detail="Interrupted"
    ))
    state = bus.get_cycle_state(cycle_id)
    assert state.status == "interrupted"
    assert state.finished_at == "2026-06-08T12:22:00Z"
