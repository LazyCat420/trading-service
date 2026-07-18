"""
Service Health Probe — Pre-cycle connectivity verification.

Runs before each trading cycle to verify all service connections.
Results are logged to cycle_audit_log, emitted as SSE events, and
written to the cycle JSONL log.

Usage:
    from app.services.logging.service_health_probe import run_all_probes
    results = await run_all_probes()
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# TODO(security): Health probes use internal network only — no user input in URLs.
# All probe targets are hardcoded from config, not from request parameters.

_probe_client: httpx.AsyncClient | None = None


async def _get_probe_client() -> httpx.AsyncClient:
    global _probe_client
    if _probe_client is None or _probe_client.is_closed:
        _probe_client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _probe_client


async def _probe_http(
    name: str, url: str, method: str = "GET", headers: dict | None = None
) -> dict:
    """Probe an HTTP endpoint and return status."""
    start = time.monotonic()
    try:
        client = await _get_probe_client()
        if method == "GET":
            resp = await client.get(url, headers=headers)
        else:
            resp = await client.post(
                url, json={"type": "health_check", "probe": True}, headers=headers
            )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "service": name,
            "url": url,
            "status": "healthy" if resp.status_code < 400 else "degraded",
            "status_code": resp.status_code,
            "latency_ms": elapsed_ms,
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except httpx.TimeoutException:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "service": name,
            "url": url,
            "status": "timeout",
            "status_code": None,
            "latency_ms": elapsed_ms,
            "error": f"Connection timed out after {elapsed_ms}ms",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "service": name,
            "url": url,
            "status": "down",
            "status_code": None,
            "latency_ms": elapsed_ms,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


async def _probe_postgres() -> dict:
    """Probe PostgreSQL connectivity."""
    start = time.monotonic()
    try:
        from app.db.connection import get_db
        with get_db() as db:
            db.execute("SELECT 1").fetchone()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "service": "postgres",
            "url": "internal",
            "status": "healthy",
            "status_code": None,
            "latency_ms": elapsed_ms,
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "service": "postgres",
            "url": "internal",
            "status": "down",
            "status_code": None,
            "latency_ms": elapsed_ms,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


async def run_all_probes() -> list[dict]:
    """Run all service health probes and return results.

    Each result dict has: service, url, status, status_code, latency_ms, error, timestamp.
    Status values: 'healthy', 'degraded', 'timeout', 'down'.
    """
    from app.config.config import settings

    probes = []

    # 1. Prism service
    # Prism attributes requests by the x-project / x-username HEADERS (body
    # fields are ignored); without them the probe is filed under prism's
    # catch-all "default"/"anonymous" project.
    probes.append(_probe_http(
        "prism-service",
        f"{settings.PRISM_URL}/health",
        headers={
            "x-project": settings.PRISM_PROJECT,
            "x-username": settings.PRISM_USERNAME,
        },
    ))

    # 2. Trading-client SSE endpoint
    probes.append(_probe_http(
        "trading-client",
        f"http://{settings.DEFAULT_HOST}:8888/api/v1/status",
    ))

    # 3. vLLM providers
    if settings.PROVIDER_VLLM_1_URL:
        probes.append(_probe_http(
            f"vllm-{settings.PROVIDER_VLLM_1_NICKNAME}",
            f"{settings.PROVIDER_VLLM_1_URL}/v1/models",
        ))
    if settings.PROVIDER_VLLM_2_URL:
        probes.append(_probe_http(
            f"vllm-{settings.PROVIDER_VLLM_2_NICKNAME}",
            f"{settings.PROVIDER_VLLM_2_URL}/v1/models",
        ))
    if settings.PROVIDER_VLLM_3_URL:
        probes.append(_probe_http(
            f"vllm-{settings.PROVIDER_VLLM_3_NICKNAME}",
            f"{settings.PROVIDER_VLLM_3_URL}/v1/models",
        ))



    # 5. PostgreSQL
    probes.append(_probe_postgres())

    # Run all probes concurrently
    results = await asyncio.gather(*probes, return_exceptions=True)

    # Convert exceptions to error results
    final_results = []
    for r in results:
        if isinstance(r, Exception):
            final_results.append({
                "service": "unknown",
                "url": "unknown",
                "status": "error",
                "status_code": None,
                "latency_ms": 0,
                "error": str(r),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        else:
            final_results.append(r)

    # Log summary
    healthy = sum(1 for r in final_results if r["status"] == "healthy")
    total = len(final_results)
    logger.info(
        "[HealthProbe] %d/%d services healthy",
        healthy, total,
    )
    for r in final_results:
        if r["status"] != "healthy":
            logger.warning(
                "[HealthProbe] %s is %s: %s (%dms)",
                r["service"], r["status"], r.get("error", ""), r["latency_ms"],
            )

    return final_results


async def run_probes_and_log(cycle_id: str) -> list[dict]:
    """Run probes and log results to the cycle audit system."""
    results = await run_all_probes()

    # Log to cycle auditor
    try:
        from app.services.logging.cycle_auditor import auditor
        auditor.record(
            cycle_id=cycle_id,
            audit_type="service_health",
            details={
                "probes": results,
                "healthy_count": sum(1 for r in results if r["status"] == "healthy"),
                "total_count": len(results),
            },
        )
    except Exception as e:
        logger.debug("[HealthProbe] Failed to write audit record: %s", e)

    # Emit SSE event to trading-client
    try:
        from app.config.config import settings
        client = await _get_probe_client()
        payload = {
            "type": "system_health",
            "probes": results,
        }
        url = f"http://{settings.DEFAULT_HOST}:8888/api/v1/prism/emit"
        await client.post(url, json=payload)
    except Exception as e:
        logger.debug("[HealthProbe] Failed to emit SSE event: %s", e)

    return results
