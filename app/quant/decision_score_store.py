"""Persistence for the deterministic baseline score.

Pure MongoDB implementation for decision_scores collection.
"""

from __future__ import annotations

import json
import logging
import uuid
from app.db import mongo_store

logger = logging.getLogger(__name__)


def record_decision_score(cycle_id: str, ticker: str, score: dict) -> None:
    """Write one baseline score. Idempotent per (cycle_id, ticker)."""
    if not cycle_id or not ticker or not isinstance(score, dict):
        return
    try:
        rr = score.get("risk_reward") or {}
        mongo_store.upsert_doc(
            'decision_scores',
            {'cycle_id': cycle_id, 'ticker': ticker.strip().upper()},
            {
                'cycle_id': cycle_id,
                'ticker': ticker.strip().upper(),
                'score': score.get("score"),
                'band': score.get("band", "NOT_SCOREABLE"),
                'baseline_confidence': score.get("confidence"),
                'coverage_pct': score.get("coverage_pct"),
                'percentile': score.get("percentile"),
                'fundamental_score': score.get("fundamental_score"),
                'technical_score': score.get("technical_score"),
                'hybrid_score': score.get("hybrid_score"),
                'risk_reward': rr.get("ratio"),
                'rr_target_source': (rr.get("sources") or {}).get("target"),
                'rr_target_horizon': rr.get("target_horizon"),
                'gates_failed': score.get("gates_failed") or [],
                'gates_unknown': score.get("gates_unknown") or [],
                'detail': json.dumps(score, default=str),
            },
        )
    except Exception as e:  # noqa: BLE001 — a shadow row never blocks a cycle
        logger.warning(
            "[DecisionScore] %s/%s: could not record baseline: %s: %s",
            cycle_id, ticker, type(e).__name__, e,
        )


def attach_board_decision(cycle_id: str, ticker: str, action: str | None,
                          confidence: int | None) -> None:
    """Copy the agents' verdict onto the baseline row in MongoDB."""
    if not cycle_id or not ticker:
        return
    try:
        mongo_store.update_docs(
            'decision_scores',
            {'cycle_id': cycle_id, 'ticker': ticker.strip().upper()},
            {'$set': {'board_action': action, 'board_confidence': confidence}},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[DecisionScore] %s/%s: could not attach board decision: %s: %s",
            cycle_id, ticker, type(e).__name__, e,
        )
