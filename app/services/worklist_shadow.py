"""Worklist shadow — pure MongoDB recording for three-way universe comparison.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from app.db import mongo_store

logger = logging.getLogger(__name__)


def record(
    cycle_id: str,
    live_tickers: Sequence[str],
    top_scorers: Sequence[dict] | None,
    worker_id: str = "",
) -> dict[str, Any]:
    """Record one three-way comparison. Never raises."""
    summary: dict[str, Any] = {"recorded": False}
    try:
        from app.services.bot_manager import get_active_bot_id
        bot_id = get_active_bot_id()
    except Exception:
        bot_id = ""

    try:
        from app.services.research_queue_service import ResearchQueueService

        live = [str(t).upper() for t in (live_tickers or [])]
        budget = len(live)
        if budget <= 0:
            return summary

        free = [
            str((s or {}).get("ticker", "")).upper()
            for s in list(top_scorers or [])[:budget]
        ]
        free = [t for t in free if t]

        try:
            queue_items = ResearchQueueService.peek_worklist(budget)
            queue = [str(i.get("ticker", "")).upper() for i in queue_items]
            queue = [t for t in queue if t]
        except Exception:
            queue = []

        try:
            depth = ResearchQueueService.get_queue_summary()
        except Exception:
            depth = {}

        live_set = set(live)
        summary = {
            "recorded": True,
            "budget": budget,
            "live": live,
            "free": free,
            "queue": queue,
            "overlap_live_free": len(live_set & set(free)),
            "overlap_live_queue": len(live_set & set(queue)),
            "queue_depth": depth,
            "queue_empty": not queue,
        }

        mongo_store.insert_docs('worklist_shadow_runs', [{
            'cycle_id': cycle_id,
            'bot_id': bot_id,
            'worker_id': worker_id,
            'budget': budget,
            'live_tickers': live,
            'free_tickers': free,
            'queue_tickers': queue,
            'queue_depth': depth,
            'overlap_live_free': summary["overlap_live_free"],
            'overlap_live_queue': summary["overlap_live_queue"],
            'queue_empty': summary["queue_empty"],
            'created_at': datetime.now(timezone.utc),
        }])

        logger.info(
            "[worklist-shadow] %s: budget=%d live∩free=%d/%d live∩queue=%d/%d "
            "queue_depth=%s",
            cycle_id, budget, summary["overlap_live_free"], budget,
            summary["overlap_live_queue"], budget, depth,
        )
    except Exception as e:  # noqa: BLE001 — a shadow never fails a cycle
        logger.warning("[worklist-shadow] %s: failed (non-fatal): %s", cycle_id, e)

    return summary
