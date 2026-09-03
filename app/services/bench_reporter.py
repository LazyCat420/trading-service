"""
DGX Spark Console Benchmark Reporter.

Reports trading cycle benchmarks, lifecycle status, and decisions to the DGX
Spark Console on Gold Spark:
    POST http://10.0.0.141:8800/api/bench/runs
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any
import httpx

logger = logging.getLogger(__name__)

SPARK_BENCH_URL = os.getenv("SPARK_BENCH_URL", "http://10.0.0.141:8800").rstrip("/")
SPARK_BENCH_ENABLED = os.getenv("SPARK_BENCH_ENABLED", "true").lower() in ("true", "1", "yes")
BENCH_TIMEOUT_S = 10.0


def build_run_id(timestamp: datetime | None = None) -> str:
    """Format an ISO-8601 UTC run ID matching the DGX Spark Console specification.

    Example: "tc-2026-09-03T21:14:05Z"
    """
    ts = timestamp or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return f"tc-{ts.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def serialize_decisions(results: list[Any] | None) -> list[dict[str, Any]]:
    """Extract and format ticker trading decisions for the Spark benchmark console."""
    decisions: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for r in (results or []):
        if not isinstance(r, dict):
            continue
        action = r.get("action")
        ticker = r.get("ticker")
        if not action or not ticker:
            continue

        confidence = r.get("confidence")
        try:
            conf_val = float(confidence) if confidence is not None else 0.0
        except (ValueError, TypeError):
            conf_val = 0.0

        rationale = r.get("rationale") or r.get("summary") or ""
        if isinstance(rationale, str):
            rationale = rationale[:300].strip()
        else:
            rationale = str(rationale)[:300]

        decisions.append({
            "ts": now_iso,
            "symbol": str(ticker).upper(),
            "action": str(action).lower(),
            "confidence": conf_val,
            "rationale": rationale,
        })

    return decisions


async def emit_bench_run(
    run_id: str,
    harness: str = "trading-cycle",
    task: str = "daily-cycle",
    started_at: str | None = None,
    ended_at: str | None = None,
    status: str = "ok",
    decisions: list[dict[str, Any]] | None = None,
    outcome: dict[str, Any] | None = None,
    notes: str | None = None,
) -> dict[str, Any] | None:
    """POST cycle execution record to the Spark Console.

    Idempotent on run_id. Fail-safe: if the Spark console is unreachable or
    times out, logs a warning and returns None without raising into the pipeline.
    """
    if not SPARK_BENCH_ENABLED:
        logger.debug("[SparkBench] Reporting disabled via SPARK_BENCH_ENABLED")
        return None

    url = f"{SPARK_BENCH_URL}/api/bench/runs"
    payload: dict[str, Any] = {
        "run_id": run_id,
        "harness": harness,
        "task": task,
        "status": status,
    }

    if started_at:
        payload["started_at"] = started_at
    if ended_at:
        payload["ended_at"] = ended_at
    if decisions is not None:
        payload["decisions"] = decisions
    if outcome is not None:
        payload["outcome"] = outcome
    if notes is not None:
        payload["notes"] = notes

    headers = {"Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=BENCH_TIMEOUT_S) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            logger.info("[SparkBench] Emitted benchmark run %s to %s (status=%s)", run_id, url, status)
            return data
    except httpx.HTTPStatusError as http_err:
        logger.warning(
            "[SparkBench] HTTP error reporting run %s to %s: %d %s",
            run_id, url, http_err.response.status_code, http_err.response.text[:200]
        )
        return None
    except Exception as exc:
        logger.warning(
            "[SparkBench] Network/connection error reporting run %s to %s (non-fatal): %s",
            run_id, url, exc
        )
        return None
