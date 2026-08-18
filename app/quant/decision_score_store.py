"""Persistence for the deterministic baseline score.

Kept out of `decision_score.py` so the scorer stays pure and testable without a
database. Everything here is fail-open: a shadow record that cannot be written
must never take down a cycle, because the score it records changes no decision.

The reason the write happens AT ALL is that the comparison it enables is not
reconstructible after the fact. `fundamentals` is not a point-in-time panel —
rows are overwritten as vendors refresh — so re-scoring a past decision next
month scores data the desk never saw. If the row is not captured when the desk
runs, the question "did the baseline rank better than the board's confidence?"
has no data behind it, which is the same reason `decision_outcomes.models_used`
exists.
"""

from __future__ import annotations

import json
import logging
import uuid
from app.db import mongo_store

logger = logging.getLogger(__name__)


def record_decision_score(cycle_id: str, ticker: str, score: dict) -> None:
    """Write one baseline score. Idempotent per (cycle_id, ticker).

    Called at desk-build time, BEFORE any agent runs, so a desk that dies
    mid-pipeline still leaves its baseline behind. `board_action` and
    `board_confidence` stay NULL until `attach_board_decision` fills them —
    NULL meaning "no decision was reached", which is a different fact from
    HOLD and is stored differently.
    """
    if not cycle_id or not ticker or not isinstance(score, dict):
        return
    try:
        from app.db.connection import get_db

        rr = score.get("risk_reward") or {}
        with get_db() as db:
            mongo_store.update_docs('decision_scores', {'cycle_id': cycle_id, 'ticker': ticker.strip().upper()}, {'$set': {'score': score.get("score"), 'band': score.get("band", "NOT_SCOREABLE"), 'baseline_confidence': score.get("confidence"), 'coverage_pct': score.get("coverage_pct"), 'percentile': score.get("percentile"), 'fundamental_score': score.get("fundamental_score"), 'technical_score': score.get("technical_score"), 'hybrid_score': score.get("hybrid_score"), 'risk_reward': rr.get("ratio"), 'rr_target_source': (rr.get("sources") or {}).get("target"), 'rr_target_horizon': rr.get("target_horizon"), 'gates_failed': score.get("gates_failed") or [], 'gates_unknown': score.get("gates_unknown") or [], 'detail': json.dumps(score, default=str)}, '$setOnInsert': {'id': str(uuid.uuid4())}}, upsert=True)
    except Exception as e:  # noqa: BLE001 — a shadow row never blocks a cycle
        logger.warning("[DecisionScore] %s/%s: could not record baseline: "
                       "%s: %s", cycle_id, ticker, type(e).__name__, e)


def attach_board_decision(cycle_id: str, ticker: str, action: str | None,
                          confidence: int | None) -> None:
    """Copy the agents' verdict onto the baseline row.

    Deliberately an UPDATE of an existing row and never an INSERT: a decision
    with no baseline row means the scorer did not run for that desk, and
    manufacturing a row here would hide that. The `UPDATE ... WHERE` simply
    matches nothing, and the missing baseline stays visible as a missing row.
    """
    if not cycle_id or not ticker:
        return
    try:
        from app.db.connection import get_db

        with get_db() as db:
            mongo_store.update_docs('decision_scores', {'cycle_id': cycle_id, 'ticker': ticker.strip().upper()}, {'$set': {'board_action': action, 'board_confidence': confidence}})
    except Exception as e:  # noqa: BLE001
        logger.warning("[DecisionScore] %s/%s: could not attach board "
                       "decision: %s: %s", cycle_id, ticker,
                       type(e).__name__, e)
