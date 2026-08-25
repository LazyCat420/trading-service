"""Read-through refresh: a READ tool must answer within its own deadline.

MEASURED 2026-08-25 over 7 days of `agent_tool_telemetry`, two read tools were
running the collection path on every call before reading a row:

    tool                p50      p95    bridge deadline   fail rate
    get_market_data    20.9s    36.0s        30s            24%
    get_finnhub_news   36.2s    65.2s        60s            16%

Both p95s sit PAST the deadline the bridge aborts on, so the calls failed by
construction — and the work was usually redundant, because the cycle's
precollect phase had already run the same collector for the same ticker
minutes earlier (`v3_precollect_finnhub_news_ok_MSTR` is in the same cycle's
event stream).

The rule this module enforces: **read first, refresh only when the store
cannot answer, and never let the refresh outlive the tool's deadline.** A
refresh that runs long is abandoned and the stored answer is returned instead
— a slightly stale answer beats an aborted one, which is worth nothing at all.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Well under EXECUTION_TIMEOUT_MS (30s) in lazy-agent-service's bridge, which
# is the tightest deadline any of these tools runs under. Leaves room for the
# read, the grounded-facts pass and the response assembly that follow.
DEFAULT_REFRESH_BUDGET_S = 12.0


async def refresh_within_budget(
    label: str,
    refresh: Callable[[], Awaitable[object]],
    budget_s: float = DEFAULT_REFRESH_BUDGET_S,
) -> bool:
    """Best-effort refresh. Returns True if it finished inside the budget.

    Never raises: a read tool must still answer from the store when the
    network is slow, rate-limited or down.
    """
    try:
        await asyncio.wait_for(refresh(), timeout=budget_s)
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "[read_through] %s refresh exceeded %.0fs — answering from the store",
            label, budget_s,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the read
        logger.warning("[read_through] %s refresh failed (%s) — answering from the store", label, exc)
        return False


def age_hours(value: datetime | None) -> float | None:
    """Hours since `value`, tolerating naive datetimes. None if unknown."""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value).total_seconds() / 3600.0


def store_can_answer(newest: datetime | None, max_age_h: float, have_rows: bool = True) -> bool:
    """Is the stored data good enough to skip the network entirely?"""
    if not have_rows:
        return False
    age = age_hours(newest)
    return age is not None and age <= max_age_h
