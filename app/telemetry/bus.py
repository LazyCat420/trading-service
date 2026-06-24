import logging
import asyncio
from typing import Any
from app.telemetry.schema import TelemetryEvent
from app.telemetry.state import CycleState

logger = logging.getLogger(__name__)

# In-memory store of cycle states: cycle_id -> CycleState
_cycle_states: dict[str, CycleState] = {}

# Active stream subscribers (asyncio.Queues)
_subscribers: list[asyncio.Queue] = []

def subscribe() -> asyncio.Queue:
    """Subscribe to the real-time telemetry event stream."""
    q = asyncio.Queue()
    _subscribers.append(q)
    return q

def unsubscribe(q: asyncio.Queue):
    """Unsubscribe from the real-time telemetry event stream."""
    if q in _subscribers:
        _subscribers.remove(q)

def get_cycle_state(cycle_id: str) -> CycleState:
    """Retrieve or reconstruct the CycleState for a given cycle ID."""
    if not cycle_id:
        return CycleState(cycle_id="")

    if cycle_id not in _cycle_states:
        # Attempt to populate from memory cache (PipelineService/CycleEngine/Mixin) or fallback to DB
        try:
            mem_state = None
            try:
                from app.services.pipeline_service import PipelineService
                if PipelineService._state and PipelineService._state.get("cycle_id") == cycle_id:
                    mem_state = PipelineService._state
            except Exception:
                pass

            if not mem_state:
                try:
                    from cycle_main import CycleEngine
                    if CycleEngine._state and CycleEngine._state.get("cycle_id") == cycle_id:
                        mem_state = CycleEngine._state
                except Exception:
                    pass

            if not mem_state:
                try:
                    if PipelineStateMixin._state and PipelineStateMixin._state.get("cycle_id") == cycle_id:
                        mem_state = PipelineStateMixin._state
                except Exception:
                    pass

            if mem_state and mem_state.get("cycle_id") == cycle_id:
                state = CycleState(
                    cycle_id=cycle_id,
                    status=mem_state.get("status", "idle"),
                    phase=mem_state.get("phase", ""),
                    progress=mem_state.get("progress", ""),
                    tickers=mem_state.get("tickers", []),
                    results=mem_state.get("results", []),
                    events=mem_state.get("events", []),
                    started_at=mem_state.get("started_at"),
                    finished_at=mem_state.get("finished_at"),
                )
                logger.info("[TelemetryBus] Restored cycle state %s from in-memory state", cycle_id)
            else:
                # Load from database fallback
                from app.services.pipeline_state import PipelineStateDB
                db_state = PipelineStateDB.get_state(summary_only=False)
                if db_state and db_state.get("cycle_id") == cycle_id:
                    state = CycleState(
                        cycle_id=cycle_id,
                        status=db_state.get("status", "idle"),
                        phase=db_state.get("phase", ""),
                        progress=db_state.get("progress", ""),
                        tickers=db_state.get("tickers", []),
                        results=db_state.get("results", []),
                        events=db_state.get("events", []),
                        started_at=db_state.get("started_at"),
                        finished_at=db_state.get("finished_at"),
                    )
                    logger.info("[TelemetryBus] Restored cycle state %s from PipelineStateDB", cycle_id)
                else:
                    state = CycleState(cycle_id=cycle_id)
        except Exception as e:
            logger.error("[TelemetryBus] Failed to restore cycle state for %s: %s", cycle_id, e)
            state = CycleState(cycle_id=cycle_id)
        
        _cycle_states[cycle_id] = state

    return _cycle_states[cycle_id]

def publish_event(event: TelemetryEvent):
    """Publish a telemetry event, updating the CycleState and notifying subscribers."""
    cycle_id = event.cycle_id
    if not cycle_id:
        return

    state = get_cycle_state(cycle_id)
    
    # Auto-fill phase from cycle state if empty
    if not event.phase and state.phase:
        event.phase = state.phase

    event_dict = event.to_dict()

    # De-duplicate events by checking ts and step/detail
    if not any(e.get("ts") == event_dict["ts"] and e.get("step") == event_dict["step"] for e in state.events):
        state.events.append(event_dict)

    # Automatically set started_at/finished_at if not populated
    if not state.started_at and event.step == "init":
        state.started_at = event.ts
    
    # Update state fields
    if event.kind == "pipeline":
        # Only allow known lifecycle statuses to overwrite the cycle-level status.
        # Event-level statuses like "streaming", "cached", "skipped", "warning"
        # are metadata for individual events and must NOT contaminate the cycle
        # status — otherwise the frontend sees an unknown status, treats it as
        # idle, and hides the Stop button (causing 409 errors on re-start).
        _LIFECYCLE_STATUSES = {
            "running", "done", "stopped", "error", "interrupted",
            "paused", "starting", "stopping", "idle",
        }
        if event.status and event.status != "ok" and event.status in _LIFECYCLE_STATUSES:
            state.status = event.status
            if event.status in ("done", "error", "stopped", "interrupted"):
                state.finished_at = event.ts

        else:
            # Update status for special stages
            if event.phase in ("started", "collecting", "analyzing", "gated", "traded", "persisted", "evaluated", "resumed") or event.step == "init":
                state.status = "running"
            elif event.phase in ("done", "closed"):
                state.status = "done"
                state.finished_at = event.ts
            elif event.phase == "stopped":
                state.status = "stopped"
                state.finished_at = event.ts
            elif event.phase == "error":
                state.status = "error"
                state.finished_at = event.ts
            elif event.phase == "interrupted":
                state.status = "interrupted"
                state.finished_at = event.ts
            elif event.phase == "paused":
                state.status = "paused"
        
        if event.phase:
            state.phase = event.phase
        if event.detail:
            state.progress = event.detail

    elif event.kind == "heartbeat":
        state.progress = event.detail

    # Extract dynamic inputs from event details/data
    if "tickers" in event.data:
        state.tickers = event.data["tickers"]
    
    if "result" in event.data:
        res = event.data["result"]
        ticker = res.get("ticker")
        if ticker and not any(r.get("ticker") == ticker for r in state.results):
            state.results.append(res)

    # Broadcast event to subscribers
    for q in _subscribers:
        try:
            q.put_nowait(event_dict)
        except Exception as e:
            logger.debug("[TelemetryBus] Failed to push to subscriber queue: %s", e)
