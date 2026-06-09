import httpx
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

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
        # Attempt to forward to the trading-client API server
        for host in ["trading-client", "localhost", "127.0.0.1"]:
            url = f"http://{host}:8888/api/v1/system/log-event"
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        break
            except Exception:
                pass
                
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
