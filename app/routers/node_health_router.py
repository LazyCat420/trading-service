"""Node Health Router — Per-box LLM endpoint connectivity and throughput.

Returns per-node health status independent of cycle state:
  GET /node-health → { "dgx_spark": {...}, "jetson": {...}, ... }
"""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()
security = HTTPBearer()


def _verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    from fastapi import HTTPException

    if credentials.credentials != settings.API_SERVER_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Server Key")
    return credentials.credentials


@router.get("/node-health")
async def node_health(token: str = Depends(_verify_api_key)):
    """Return per-node LLM endpoint health, independent of cycle state."""
    try:
        import asyncio
        from app.services.prism_agent_caller import llm
        from app.monitoring.llm_tracker import tracker

        endpoints = getattr(llm, "_endpoints", {})

        # Dynamically sync/probe model names from all enabled endpoints before reporting stats
        await asyncio.gather(*[
            llm._sync_endpoint_model(ep, force=False)
            for ep in endpoints.values()
            if ep is not None and getattr(ep, "enabled", False)
        ], return_exceptions=True)

        tps_by_endpoint = tracker.get_recent_tps_by_endpoint(window_seconds=60)

        result = {}
        for name, ep in endpoints.items():
            if ep is None:
                continue

            is_enabled = getattr(ep, "enabled", False)
            is_online = False
            model_name = None
            active = 0
            queued = 0
            max_concurrent = 0

            try:
                model_name = getattr(ep, "model", None)
                is_online = is_enabled and model_name is not None

                # Active/queued from semaphore
                sem = getattr(ep, "_semaphore", None)
                if sem is not None:
                    max_concurrent = getattr(sem, "_value", 0) + getattr(
                        sem, "_waiters", []
                    ).__len__()
                    # For asyncio.Semaphore, _value is remaining capacity
                    bound = getattr(ep, "max_concurrent", None)
                    if bound:
                        max_concurrent = bound
                        active = max(0, bound - sem._value)
                else:
                    max_concurrent = getattr(ep, "max_concurrent", 0) or 0
                    active = max(getattr(ep, "active_count", 0), getattr(ep, "requests_running", 0))

                queue_obj = getattr(ep, "queue", None)
                qsize = queue_obj.qsize() if queue_obj else 0
                queued = max(qsize, getattr(ep, "requests_waiting", 0))
            except Exception as e:
                logger.debug(
                    "[node-health] Error reading endpoint %s: %s", name, e
                )

            # TPS from the tracker
            tps = tps_by_endpoint.get(name, 0.0)

            # Last seen: use the most recent call timestamp from tracker
            last_seen = None
            try:
                for call in reversed(list(tracker._history)):
                    if call.endpoint_name == name:
                        last_seen = call.timestamp
                        break
            except Exception:
                pass

            result[name] = {
                "connected": is_online,
                "enabled": is_enabled,
                "model": model_name,
                "tps": round(tps, 1),
                "active": active,
                "queued": queued,
                "max_concurrent": max_concurrent,
                "last_seen": last_seen,
            }

        # Ensure we always have jetson and dgx_spark keys for the frontend
        for key in ("jetson", "dgx_spark"):
            if key not in result:
                result[key] = {
                    "connected": False,
                    "enabled": False,
                    "model": None,
                    "tps": 0,
                    "active": 0,
                    "queued": 0,
                    "max_concurrent": 0,
                    "last_seen": None,
                }

        return result

    except Exception as e:
        logger.error("[node-health] Failed: %s", e)
        return {
            "jetson": {
                "connected": False,
                "enabled": False,
                "model": None,
                "tps": 0,
                "active": 0,
                "queued": 0,
                "max_concurrent": 0,
                "last_seen": None,
                "error": str(e),
            },
            "dgx_spark": {
                "connected": False,
                "enabled": False,
                "model": None,
                "tps": 0,
                "active": 0,
                "queued": 0,
                "max_concurrent": 0,
                "last_seen": None,
                "error": str(e),
            },
        }
