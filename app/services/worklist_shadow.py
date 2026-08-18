"""Worklist shadow — would the research queue pick a different universe?

## The question

Ticker selection today is: score every candidate arithmetically, cap at 2 per
sector, keep the top 20, then ask an LLM gatekeeper to subset it. The
gatekeeper's whole marginal contribution is dropping ~8 of 20 pre-ranked,
pre-diversified names, and no measurement anywhere shows it beats
`top_scorers[:N]`.

The research queue proposes a third answer: pick the tickers the desk has
unanswered questions about, positions to review, and triggers to check. That is
a different *kind* of selection — driven by the system's own open questions
rather than by a screen re-run from scratch each cycle.

Before any of that touches a live cycle it has to be compared, so this records
all three side by side and changes nothing:

    live      what the cycle actually analysed
    free      top_scorers[:N] — the free baseline the gatekeeper never beat
    queue     what peek_worklist would have chosen

## Invariants, borrowed from model_shadow

1. **It cannot change the cycle.** Nothing reads its output back.
2. **It cannot fail the cycle.** Every exception is swallowed.
3. **It cannot consume the queue.** `peek_worklist`, never `pop_worklist` —
   a pop would move rows to `processing` with no worker serving them, so the
   shadow would drain the thing it is measuring.

Rows carry `bot_id` from the start. `model_shadow_runs` shipped without one and
open item 1e records the cost: a shadow row that cannot say where it came from
blocks the evidence it was accruing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from app.db.connection import get_db
from app.db import mongo_store

logger = logging.getLogger(__name__)


def _ensure_table() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS worklist_shadow_runs (
                id              BIGSERIAL PRIMARY KEY,
                cycle_id        TEXT NOT NULL,
                bot_id          TEXT,
                worker_id       TEXT,
                created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                budget          INTEGER,
                live_tickers    JSONB DEFAULT '[]',
                free_tickers    JSONB DEFAULT '[]',
                queue_tickers   JSONB DEFAULT '[]',
                queue_depth     JSONB DEFAULT '{}',
                overlap_live_free   INTEGER,
                overlap_live_queue  INTEGER,
                queue_empty     BOOLEAN DEFAULT TRUE
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_worklist_shadow_cycle "
            "ON worklist_shadow_runs (cycle_id, created_at DESC)"
        )


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

        queue_items = ResearchQueueService.peek_worklist(budget)
        queue = [str(i.get("ticker", "")).upper() for i in queue_items]
        queue = [t for t in queue if t]

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
            # An empty queue is the expected state until the dossier sync has
            # run for a cycle. Recorded explicitly so a later reader does not
            # mistake "nothing enqueued yet" for "the queue agrees with live".
            "queue_empty": not queue,
        }

        _ensure_table()
        with get_db() as db:
            mongo_store.insert_docs('worklist_shadow_runs', [{'cycle_id': cycle_id, 'bot_id': bot_id, 'worker_id': worker_id, 'budget': budget, 'live_tickers': json.dumps(live), 'free_tickers': json.dumps(free), 'queue_tickers': json.dumps(queue), 'queue_depth': json.dumps(depth), 'overlap_live_free': summary["overlap_live_free"], 'overlap_live_queue': summary["overlap_live_queue"], 'queue_empty': summary["queue_empty"]}])

        logger.info(
            "[worklist-shadow] %s: budget=%d live∩free=%d/%d live∩queue=%d/%d "
            "queue_depth=%s",
            cycle_id, budget, summary["overlap_live_free"], budget,
            summary["overlap_live_queue"], budget, depth,
        )
    except Exception as e:  # noqa: BLE001 — a shadow never fails a cycle
        logger.warning("[worklist-shadow] %s: failed (non-fatal): %s", cycle_id, e)

    return summary
