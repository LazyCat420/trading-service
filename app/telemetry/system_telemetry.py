import os
import httpx
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# trading-client is NOT on trading-service's docker network, so the
# "trading-client" hostname doesn't resolve in-container and localhost points
# at trading-service itself — logs were silently dropped and the office/logs
# UI saw nothing. The NAS host IP is the address that actually works; allow
# override via TRADING_CLIENT_URL (e.g. "http://10.0.0.16:8888").
_CANDIDATE_BASES = [
    base
    for base in (
        os.getenv("TRADING_CLIENT_URL", "").rstrip("/") or None,
        "http://trading-client:8888",
        "http://10.0.0.16:8888",
        "http://localhost:8888",
    )
    if base
]

_working_base: str | None = None
_warned_unreachable = False


def send_system_log(subsystem: str, message: str, level: str = "info"):
    """
    Publish a lightweight system telemetry event to the trading-client server.
    This runs asynchronously in the background.
    """
    payload = {
        "subsystem": subsystem.upper(),
        "message": message,
        "level": level.lower(),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    async def _send():
        global _working_base, _warned_unreachable
        bases = [_working_base] if _working_base else _CANDIDATE_BASES
        for base in bases:
            url = f"{base}/api/v1/system/log-event"
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        _working_base = base
                        return
            except Exception:
                continue
        # All candidates failed — drop the event, but say so once so the
        # office/logs UI going dark is diagnosable instead of silent.
        _working_base = None
        if not _warned_unreachable:
            _warned_unreachable = True
            logger.warning(
                "[telemetry] trading-client unreachable at %s — system log "
                "events are being dropped (set TRADING_CLIENT_URL to fix)",
                _CANDIDATE_BASES,
            )

    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            loop.create_task(_send())
        else:
            # Fallback if loop is not running
            asyncio.run(_send())
    except RuntimeError:
        # No event loop in this thread, try running with asyncio.run
        try:
            asyncio.run(_send())
        except Exception:
            pass
