"""
Monitoring Dashboard — FastAPI router with endpoints for observing vLLM calls.

Mount this on your FastAPI app or run standalone.
"""

import asyncio
import json
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse, PlainTextResponse
from app.monitoring.llm_tracker import tracker
from app.monitoring.metrics_collector import metrics
from app.monitoring.pipeline_profiler import profiler as pipeline_profiler
from app.services.prism_agent_caller import llm
from app.config import settings
from app.db.connection import get_db
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitor", tags=["monitoring"])


@router.get("/health")
async def monitor_health():
    """Jetson health + model info."""
    healthy = await llm.health()
    models = []
    try:
        models = await llm.list_models()
    except Exception:
        pass
    return {
        "vllm_healthy": healthy,
        "vllm_url": settings.PROVIDER_VLLM_1_URL,
        "model": llm.model or "Auto-discovering...",
        "loaded_models": models,
        "semaphore_max_jetson": settings.JETSON_MAX_CONCURRENT,
        "semaphore_max_dgx": settings.DGX_MAX_CONCURRENT,
        "semaphore_active": llm._active_slots,
    }


@router.get("/stats")
async def monitor_stats():
    """Aggregate stats across all LLM calls."""
    stats = tracker.get_stats()
    stats["recent_tps"] = tracker.get_recent_tps(60)
    stats["recent_tps_by_endpoint"] = tracker.get_recent_tps_by_endpoint(60)
    latest_metrics = metrics.get_latest()
    return {
        "llm_stats": stats,
        "jetson_metrics": latest_metrics,
    }


@router.get("/calls")
async def monitor_calls(
    limit: int = Query(default=50, le=1000),
    agent: str | None = Query(default=None),
):
    """Recent LLM calls with full prompt/response."""
    return tracker.get_calls(limit=limit, agent=agent)


@router.get("/calls/{call_id}")
async def monitor_call_detail(call_id: str):
    """Single call detail."""
    call = tracker.get_call(call_id)
    if not call:
        return {"error": "Call not found"}
    return call


@router.get("/agents")
async def monitor_agents():
    """Per-agent stats breakdown."""
    return tracker.get_agent_stats()


@router.get("/metrics")
async def monitor_jetson_metrics():
    """Latest Jetson GPU/KV metrics snapshot."""
    snapshot = await metrics.collect_once()
    if snapshot:
        return snapshot.to_dict()
    return {"error": "Failed to collect metrics"}


@router.get("/metrics/history")
async def monitor_metrics_history(
    limit: int = Query(default=60, le=360),
):
    """Time-series metrics for graphing."""
    return metrics.get_history(limit=limit)


@router.get("/telemetry/charts")
async def monitor_telemetry_charts(hours: int = 48):
    """Historical chart data for LLM tokens and model stats."""
    with get_db() as db:
        try:
            # Tokens Timeline
            db.execute(
                """
                SELECT 
                    date_trunc('hour', created_at) as hour,
                    COALESCE(endpoint_name, model) as endpoint_or_model,
                    SUM(tokens_used) as total_tokens,
                    COUNT(*) as request_count
                FROM llm_audit_logs
                WHERE created_at >= NOW() - INTERVAL '1 hour' * %s
                GROUP BY 1, 2
                ORDER BY 1 ASC
                """,
                [hours],
            )
            timeline_rows = db.fetchall()
            timeline = []
            if timeline_rows:
                cols = [desc[0] for desc in db.description]
                for row in timeline_rows:
                    timeline.append(dict(zip(cols, row)))

            # Model Stats
            db.execute(
                """
                SELECT 
                    COALESCE(endpoint_name, model) as endpoint_or_model,
                    model,
                    COUNT(*) as total_requests,
                    SUM(tokens_used) as total_tokens,
                    AVG(execution_ms) as avg_latency_ms,
                    AVG(tokens_per_second) as avg_tps
                FROM llm_audit_logs
                WHERE created_at >= NOW() - INTERVAL '1 hour' * %s
                GROUP BY 1, 2
                ORDER BY 2 DESC
                """,
                [hours],
            )
            stats_rows = db.fetchall()
            model_stats = []
            if stats_rows:
                cols = [desc[0] for desc in db.description]
                for row in stats_rows:
                    model_stats.append(dict(zip(cols, row)))

            return {"timeline": timeline, "model_stats": model_stats}
        except Exception as e:
            logger.error(f"[Monitor] Failed to fetch telemetry charts: {e}")
            return {"timeline": [], "model_stats": []}


@router.get("/stream")
async def monitor_stream():
    """SSE stream of live LLM calls as they happen."""
    queue = tracker.subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=30.0)
                    data = json.dumps(
                        {
                            "call_id": record.call_id,
                            "timestamp": record.timestamp,
                            "agent": record.agent_name,
                            "ticker": record.ticker,
                            "prompt_tokens": record.prompt_tokens,
                            "completion_tokens": record.completion_tokens,
                            "latency_ms": record.latency_ms,
                            "success": record.success,
                            "summary": record.summary,
                        }
                    )
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield ": keepalive\n\n"
        finally:
            tracker.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ── Pipeline Profiler ──────────────────────────────────────────────────


@router.get("/profiler")
async def monitor_profiler():
    """Phase-level timing breakdown for current/last cycle."""
    return pipeline_profiler.get_report()


@router.get("/profiler/active")
async def monitor_profiler_active():
    """Currently running phases (for live monitoring)."""
    return pipeline_profiler.get_active_phases()


@router.get("/profiler/gantt")
async def monitor_profiler_gantt():
    """ASCII Gantt chart of phase timings."""
    chart = pipeline_profiler.gantt_chart()
    return PlainTextResponse(chart or "No profiling data available.")


@router.get("/profiler/history")
async def monitor_profiler_history(
    limit: int = Query(default=10, le=50),
):
    """Cross-cycle timing comparison."""
    return pipeline_profiler.get_history(limit=limit)


@router.get("/queue")
async def monitor_queue_status():
    """Priority queue and slot utilization for Jetson + DGX."""
    return llm.queue_status()


@router.get("/concurrency")
async def monitor_concurrency():
    """Adaptive concurrency controller status."""
    try:
        from app.services.adaptive_concurrency import concurrency_controller
        return concurrency_controller.status()
    except Exception as e:
        return {"error": str(e)}


# ── Agent Audit Endpoints ──────────────────────────────────────────────


@router.get("/audit")
async def monitor_audit(limit: int = Query(default=50, le=200)):
    """Agent audit summary + recent events."""
    from app.monitoring.audit_middleware import (
        get_audit_summary,
        get_audit_buffer,
    )
    summary = get_audit_summary()
    summary["recent_events"] = get_audit_buffer(limit=limit)
    return summary


@router.get("/audit/warnings")
async def monitor_audit_warnings(limit: int = Query(default=50, le=200)):
    """Recent audit warnings (slow DB, truncation, fallback, overflow)."""
    from app.monitoring.audit_middleware import get_audit_warnings
    return {"warnings": get_audit_warnings(limit=limit)}


@router.get("/audit/db")
async def monitor_audit_db(
    limit: int = Query(default=50, le=500),
    agent: str | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    hours: int = Query(default=24, le=168),
):
    """Query persisted audit events from the database."""
    try:
        query = (
            "SELECT request_id, endpoint, agent_name, model_used, "
            "system_prompt_hash, context_build_ms, inference_ms, "
            "tokens_input, tokens_output, tokens_total, "
            "is_truncated, fallback_triggered, circuit_breaker_open, "
            "ticker, cycle_id, status, detail, created_at "
            "FROM agent_audit_log "
            "WHERE created_at >= NOW() - INTERVAL '1 hour' * %s"
        )
        params: list = [hours]

        if agent:
            query += " AND agent_name = %s"
            params.append(agent)
        if endpoint:
            query += " AND endpoint = %s"
            params.append(endpoint)

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        with get_db() as db:
            rows = db.execute(query, params).fetchall()
            cols = [
                "request_id", "endpoint", "agent_name", "model_used",
                "system_prompt_hash", "context_build_ms", "inference_ms",
                "tokens_input", "tokens_output", "tokens_total",
                "is_truncated", "fallback_triggered", "circuit_breaker_open",
                "ticker", "cycle_id", "status", "detail", "created_at",
            ]
            return {
                "count": len(rows),
                "events": [dict(zip(cols, r)) for r in rows],
            }
    except Exception as e:
        logger.error("[Monitor] Audit DB query failed: %s", e)
        return {"count": 0, "events": [], "error": str(e)}


@router.get("/audit/worker")
async def monitor_audit_worker():
    """Background audit worker status (memory, connections, distribution)."""
    try:
        from app.monitoring.audit_worker import get_worker_status
        return get_worker_status()
    except Exception as e:
        return {"error": str(e)}

