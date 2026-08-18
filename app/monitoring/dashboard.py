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
from app.db import mongo_query, mongo_store
from datetime import datetime, timedelta, timezone
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
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        # COALESCE(endpoint_name, model): $ifNull also folds a MISSING field,
        # which matches Postgres NULL semantics for an absent column.
        endpoint_or_model = {"$ifNull": ["$endpoint_name", "$model"]}

        # Tokens Timeline
        timeline_docs = mongo_store.aggregate(
            "llm_audit_logs",
            [
                {"$match": {"created_at": {"$gte": cutoff}}},
                {
                    "$group": {
                        "_id": {
                            "hour": {
                                "$dateTrunc": {"date": "$created_at", "unit": "hour"}
                            },
                            "endpoint_or_model": endpoint_or_model,
                        },
                        "total_tokens": {"$sum": "$tokens_used"},
                        "request_count": {"$sum": 1},
                    }
                },
                {"$sort": {"_id.hour": 1}},
            ],
        )
        timeline = [
            {
                "hour": d["_id"]["hour"],
                "endpoint_or_model": d["_id"]["endpoint_or_model"],
                "total_tokens": d.get("total_tokens"),
                "request_count": d.get("request_count"),
            }
            for d in timeline_docs
        ]

        # Model Stats
        stats_docs = mongo_store.aggregate(
            "llm_audit_logs",
            [
                {"$match": {"created_at": {"$gte": cutoff}}},
                {
                    "$group": {
                        "_id": {
                            "endpoint_or_model": endpoint_or_model,
                            "model": "$model",
                        },
                        "total_requests": {"$sum": 1},
                        "total_tokens": {"$sum": "$tokens_used"},
                        "avg_latency_ms": {"$avg": "$execution_ms"},
                        "avg_tps": {"$avg": "$tokens_per_second"},
                    }
                },
                {"$sort": {"_id.model": -1}},
            ],
        )
        model_stats = [
            {
                "endpoint_or_model": d["_id"]["endpoint_or_model"],
                "model": d["_id"]["model"],
                "total_requests": d.get("total_requests"),
                "total_tokens": d.get("total_tokens"),
                "avg_latency_ms": d.get("avg_latency_ms"),
                "avg_tps": d.get("avg_tps"),
            }
            for d in stats_docs
        ]

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
        mquery: dict = {
            "created_at": {
                "$gte": datetime.now(timezone.utc) - timedelta(hours=hours)
            }
        }
        if agent:
            mquery["agent_name"] = agent
        if endpoint:
            mquery["endpoint"] = endpoint

        cols = [
            "request_id", "endpoint", "agent_name", "model_used",
            "system_prompt_hash", "context_build_ms", "inference_ms",
            "tokens_input", "tokens_output", "tokens_total",
            "is_truncated", "fallback_triggered", "circuit_breaker_open",
            "ticker", "cycle_id", "status", "detail", "created_at",
        ]
        rows = mongo_query.find_rows(
            "agent_audit_log", mquery, cols,
            sort=[("created_at", -1)], limit=limit,
        )
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

