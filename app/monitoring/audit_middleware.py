"""
Agent Audit Middleware — Decorator + utilities for auditing every /chat and /agent LLM call.

Tracks:
  - Context-build latency (Slow DB detection)
  - Inference latency + token usage
  - Prompt hashing (Prompt Snapshot drift detection)
  - Context window overflow warnings
  - Prism fallback / circuit breaker events
  - Response truncation detection

Usage:
    from app.monitoring.audit_middleware import audit_agent_call, log_audit_event

    # Decorator on any async LLM-calling function:
    @audit_agent_call(agent_name="user_chat", endpoint="/chat")
    async def my_llm_function(...):
        ...

    # Direct event logging:
    log_audit_event(endpoint="/chat", agent_name="user_chat", ...)
"""

import functools
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────
SLOW_DB_THRESHOLD_MS = 2000       # Context build > 2s = warning
CONTEXT_OVERFLOW_PCT = 0.90       # > 90% of context window = warning
TRUNCATION_MARKERS = [
    "⚠️ The model's response was cut short",
    "response was cut short",
]

# ── In-memory ring buffer for fast dashboard queries ──────────────────
from collections import deque

_AUDIT_BUFFER: deque[dict] = deque(maxlen=500)
_AUDIT_WARNINGS: deque[dict] = deque(maxlen=200)


def log_audit_event(
    *,
    request_id: str = "",
    endpoint: str = "",
    agent_name: str = "",
    model_used: str = "",
    system_prompt_hash: str = "",
    context_build_ms: int = 0,
    inference_ms: int = 0,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_total: int = 0,
    is_truncated: bool = False,
    fallback_triggered: bool = False,
    circuit_breaker_open: bool = False,
    ticker: str = "",
    cycle_id: str = "",
    status: str = "ok",
    detail: str = "",
    extra: dict[str, Any] | None = None,
) -> dict:
    """Log a single audit event to the in-memory buffer and DB.

    Returns the event dict for chaining/inspection.
    """
    if not request_id:
        request_id = str(uuid.uuid4())

    event = {
        "request_id": request_id,
        "endpoint": endpoint,
        "agent_name": agent_name,
        "model_used": model_used,
        "system_prompt_hash": system_prompt_hash,
        "context_build_ms": context_build_ms,
        "inference_ms": inference_ms,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "tokens_total": tokens_total,
        "is_truncated": is_truncated,
        "fallback_triggered": fallback_triggered,
        "circuit_breaker_open": circuit_breaker_open,
        "ticker": ticker,
        "cycle_id": cycle_id,
        "status": status,
        "detail": detail,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        event["extra"] = extra

    # Push to ring buffer
    _AUDIT_BUFFER.append(event)

    # Detect warnings
    warnings_list = []
    if context_build_ms > SLOW_DB_THRESHOLD_MS:
        warnings_list.append({
            "type": "Slow_DB_Warning",
            "detail": f"Context build took {context_build_ms}ms (threshold: {SLOW_DB_THRESHOLD_MS}ms)",
            "agent_name": agent_name,
            "endpoint": endpoint,
            "ts": event["created_at"],
        })
    if is_truncated:
        warnings_list.append({
            "type": "Inference_Truncation_Warning",
            "detail": f"Response was truncated for {agent_name} on {endpoint}",
            "agent_name": agent_name,
            "endpoint": endpoint,
            "ts": event["created_at"],
        })
    if fallback_triggered:
        warnings_list.append({
            "type": "Prism_Fallback_Warning",
            "detail": f"Prism failed, fell back to local vLLM for {agent_name}",
            "agent_name": agent_name,
            "endpoint": endpoint,
            "ts": event["created_at"],
        })
    if circuit_breaker_open:
        warnings_list.append({
            "type": "Circuit_Breaker_Open",
            "detail": f"Prism circuit breaker is OPEN — all calls routing to local vLLM",
            "agent_name": agent_name,
            "endpoint": endpoint,
            "ts": event["created_at"],
        })

    for w in warnings_list:
        _AUDIT_WARNINGS.append(w)
        logger.warning("[AgentAudit] %s: %s", w["type"], w["detail"])

    # Persist to DB (best-effort, non-blocking)
    _persist_audit_event(event)

    # Publish to telemetry bus
    try:
        from app.telemetry.bus import publish_event
        from app.telemetry.schema import TelemetryEvent

        publish_event(TelemetryEvent(
            ts=event["created_at"],
            cycle_id=cycle_id,
            ticker=ticker,
            kind="audit",
            source=endpoint or "agent",
            status=status,
            step="agent_audit",
            detail=detail or f"{agent_name} on {endpoint}",
            elapsed_ms=inference_ms,
            data={
                "context_build_ms": context_build_ms,
                "tokens_input": tokens_input,
                "tokens_output": tokens_output,
                "is_truncated": is_truncated,
                "fallback_triggered": fallback_triggered,
                "prompt_hash": system_prompt_hash,
            }
        ))
    except Exception as tel_err:
        logger.debug("[AgentAudit] Telemetry publish failed: %s", tel_err)

    return event


def _persist_audit_event(event: dict):
    """Best-effort write to agent_audit_log table."""
    try:
        from app.db.connection import get_db
        with get_db() as db:
            db.execute(
                """INSERT INTO agent_audit_log
                   (request_id, endpoint, agent_name, model_used,
                    system_prompt_hash, context_build_ms, inference_ms,
                    tokens_input, tokens_output, tokens_total,
                    is_truncated, fallback_triggered, circuit_breaker_open,
                    ticker, cycle_id, status, detail, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [
                    event["request_id"],
                    event["endpoint"],
                    event["agent_name"],
                    event["model_used"],
                    event["system_prompt_hash"],
                    event["context_build_ms"],
                    event["inference_ms"],
                    event["tokens_input"],
                    event["tokens_output"],
                    event["tokens_total"],
                    event["is_truncated"],
                    event["fallback_triggered"],
                    event["circuit_breaker_open"],
                    event["ticker"],
                    event["cycle_id"],
                    event["status"],
                    event["detail"],
                    event["created_at"],
                ],
            )
        # Best-effort Mongo dual-write (natural key: request_id).
        try:
            from app.db import mongo_store
            if mongo_store.writes_mongo("agent_audit_log"):
                mongo_store.insert_docs("agent_audit_log", [dict(event)])
        except Exception:
            pass
    except Exception as e:
        logger.debug("[AgentAudit] DB persist failed (non-fatal): %s", e)


def hash_prompt(prompt: str) -> str:
    """SHA-256 hash of a prompt for snapshot drift detection."""
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:16]


def check_context_overflow(
    token_count: int,
    context_limit: int = 32768,
    agent_name: str = "",
    endpoint: str = "",
) -> bool:
    """Check if token usage exceeds the safe threshold.

    Returns True if overflow detected (and logs a warning).
    """
    if context_limit <= 0:
        return False
    ratio = token_count / context_limit
    if ratio >= CONTEXT_OVERFLOW_PCT:
        warning = {
            "type": "Context_Overflow_Warning",
            "detail": (
                f"Token usage {token_count}/{context_limit} "
                f"({ratio:.0%}) for {agent_name} on {endpoint}"
            ),
            "agent_name": agent_name,
            "endpoint": endpoint,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        _AUDIT_WARNINGS.append(warning)
        logger.warning("[AgentAudit] %s: %s", warning["type"], warning["detail"])
        return True
    return False


def check_response_truncation(response_text: str) -> bool:
    """Check if a response contains truncation markers."""
    if not response_text:
        return False
    return any(marker in response_text for marker in TRUNCATION_MARKERS)


def get_audit_buffer(limit: int = 50) -> list[dict]:
    """Return the most recent audit events from the ring buffer."""
    items = list(_AUDIT_BUFFER)
    return items[-limit:]


def get_audit_warnings(limit: int = 50) -> list[dict]:
    """Return the most recent audit warnings."""
    items = list(_AUDIT_WARNINGS)
    return items[-limit:]


def get_audit_summary() -> dict:
    """Aggregate summary of audit events in the buffer."""
    events = list(_AUDIT_BUFFER)
    if not events:
        return {
            "total_events": 0,
            "total_warnings": len(_AUDIT_WARNINGS),
            "avg_context_build_ms": 0,
            "avg_inference_ms": 0,
            "fallback_count": 0,
            "truncation_count": 0,
            "by_endpoint": {},
            "by_agent": {},
        }

    total = len(events)
    avg_ctx = sum(e.get("context_build_ms", 0) for e in events) / total
    avg_inf = sum(e.get("inference_ms", 0) for e in events) / total
    fallbacks = sum(1 for e in events if e.get("fallback_triggered"))
    truncations = sum(1 for e in events if e.get("is_truncated"))

    by_endpoint: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    for e in events:
        ep = e.get("endpoint", "unknown")
        ag = e.get("agent_name", "unknown")
        by_endpoint[ep] = by_endpoint.get(ep, 0) + 1
        by_agent[ag] = by_agent.get(ag, 0) + 1

    return {
        "total_events": total,
        "total_warnings": len(_AUDIT_WARNINGS),
        "avg_context_build_ms": round(avg_ctx, 1),
        "avg_inference_ms": round(avg_inf, 1),
        "fallback_count": fallbacks,
        "truncation_count": truncations,
        "by_endpoint": by_endpoint,
        "by_agent": by_agent,
    }


def audit_agent_call(agent_name: str = "", endpoint: str = ""):
    """Decorator that wraps an async function to automatically audit its execution.

    The wrapped function should return a tuple of (response_text, token_count, elapsed_ms).
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            request_id = str(uuid.uuid4())
            start = time.monotonic()

            try:
                result = await func(*args, **kwargs)
                elapsed_ms = int((time.monotonic() - start) * 1000)

                # Try to extract metrics from the result
                response_text = ""
                tokens = 0
                if isinstance(result, tuple) and len(result) >= 2:
                    response_text = str(result[0]) if result[0] else ""
                    tokens = int(result[1]) if result[1] else 0

                is_truncated = check_response_truncation(response_text)

                log_audit_event(
                    request_id=request_id,
                    endpoint=endpoint,
                    agent_name=agent_name or kwargs.get("agent_name", ""),
                    tokens_total=tokens,
                    inference_ms=elapsed_ms,
                    is_truncated=is_truncated,
                    status="ok",
                    detail=f"Completed in {elapsed_ms}ms",
                    ticker=kwargs.get("ticker", ""),
                    cycle_id=kwargs.get("cycle_id", ""),
                )
                return result

            except Exception as e:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                log_audit_event(
                    request_id=request_id,
                    endpoint=endpoint,
                    agent_name=agent_name or kwargs.get("agent_name", ""),
                    inference_ms=elapsed_ms,
                    status="error",
                    detail=f"Failed after {elapsed_ms}ms: {type(e).__name__}: {str(e)[:200]}",
                    ticker=kwargs.get("ticker", ""),
                    cycle_id=kwargs.get("cycle_id", ""),
                )
                raise

        return wrapper
    return decorator
