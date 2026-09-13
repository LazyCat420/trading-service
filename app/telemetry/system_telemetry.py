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
#
# DEDUPLICATED. `TRADING_CLIENT_URL` is set to "http://10.0.0.16:8888" in the
# container, which is also hardcoded below — so the list held that host TWICE
# and a genuine outage paid its 3.0 s timeout against the same dead address
# three times instead of twice. Order is preserved: the env override stays
# first.
def _bases() -> list[str]:
    seen: dict[str, None] = {}
    for base in (
        os.getenv("TRADING_CLIENT_URL", "").rstrip("/") or None,
        "http://trading-client:8888",
        "http://10.0.0.16:8888",
        "http://localhost:8888",
    ):
        if base:
            seen.setdefault(base, None)
    return list(seen)


_CANDIDATE_BASES = _bases()

_working_base: str | None = None
#: Events lost since the last successful delivery. Counted rather than flagged:
#: there was no counter at all, so "how many were dropped" was unanswerable by
#: construction and the client keeps only an in-memory 200-event ring, which
#: makes the loss unmeasurable from the other end too.
_dropped_since_ok = 0
_dropped_total = 0
#: Strong references to in-flight tasks. `loop.create_task(...)` with the
#: handle discarded lets the event loop drop the task at any GC point — an
#: event lost with NO warning, which is worse than the failure this module
#: exists to report.
_inflight: set = set()


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
        global _working_base, _dropped_since_ok, _dropped_total
        bases = [_working_base] if _working_base else _CANDIDATE_BASES
        for base in bases:
            url = f"{base}/api/v1/system/log-event"
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        _working_base = base
                        if _dropped_since_ok:
                            # Say it RECOVERED. The old code latched a one-shot
                            # flag that never reset, so a single blip turned
                            # into a permanent present-tense claim that events
                            # "are being dropped" — measured once at 04:10:57
                            # with delivery resuming at 04:11:17, 20 s later,
                            # while TRADING_CLIENT_URL was set correctly the
                            # whole time. The operator was sent to fix a
                            # variable that was never wrong.
                            logger.info(
                                "[telemetry] trading-client reachable again at "
                                "%s — %d event(s) were dropped in the gap "
                                "(%d total this process)",
                                base, _dropped_since_ok, _dropped_total,
                            )
                            _dropped_since_ok = 0
                        return
            except Exception:
                continue
        # All candidates failed — drop the event, but say so once per OUTAGE so
        # the office/logs UI going dark is diagnosable instead of silent.
        _working_base = None
        _dropped_since_ok += 1
        _dropped_total += 1
        if _dropped_since_ok == 1:
            logger.warning(
                "[telemetry] trading-client unreachable at %s — dropped 1 "
                "event, retrying on the next one. If this is followed by no "
                "'reachable again' line, the UI is dark.",
                _CANDIDATE_BASES,
            )
        elif _dropped_since_ok % 50 == 0:
            logger.warning(
                "[telemetry] trading-client still unreachable — %d events "
                "dropped since the last success",
                _dropped_since_ok,
            )

    def _spawn(loop):
        task = loop.create_task(_send())
        _inflight.add(task)
        task.add_done_callback(_inflight.discard)

    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            _spawn(loop)
        else:
            # Fallback if loop is not running
            asyncio.run(_send())
    except RuntimeError:
        # No event loop in this thread, try running with asyncio.run
        try:
            asyncio.run(_send())
        except Exception:
            pass


def telemetry_drop_stats() -> dict:
    """For tests and for anything that wants to assert the UI is not dark."""
    return {
        "working_base": _working_base,
        "dropped_since_ok": _dropped_since_ok,
        "dropped_total": _dropped_total,
        "bases": list(_CANDIDATE_BASES),
    }
