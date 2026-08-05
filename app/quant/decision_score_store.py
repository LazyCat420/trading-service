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
            db.execute(
                """
                INSERT INTO decision_scores (
                    id, cycle_id, ticker, score, band, baseline_confidence,
                    coverage_pct, percentile, fundamental_score,
                    technical_score, hybrid_score, risk_reward,
                    rr_target_source, rr_target_horizon, gates_failed,
                    gates_unknown, detail
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (cycle_id, ticker) DO UPDATE SET
                    score = EXCLUDED.score,
                    band = EXCLUDED.band,
                    baseline_confidence = EXCLUDED.baseline_confidence,
                    coverage_pct = EXCLUDED.coverage_pct,
                    percentile = EXCLUDED.percentile,
                    fundamental_score = EXCLUDED.fundamental_score,
                    technical_score = EXCLUDED.technical_score,
                    hybrid_score = EXCLUDED.hybrid_score,
                    risk_reward = EXCLUDED.risk_reward,
                    rr_target_source = EXCLUDED.rr_target_source,
                    rr_target_horizon = EXCLUDED.rr_target_horizon,
                    gates_failed = EXCLUDED.gates_failed,
                    gates_unknown = EXCLUDED.gates_unknown,
                    detail = EXCLUDED.detail
                """,
                [
                    str(uuid.uuid4()), cycle_id, ticker.strip().upper(),
                    score.get("score"), score.get("band", "NOT_SCOREABLE"),
                    score.get("confidence"), score.get("coverage_pct"),
                    score.get("percentile"), score.get("fundamental_score"),
                    score.get("technical_score"), score.get("hybrid_score"),
                    rr.get("ratio"), (rr.get("sources") or {}).get("target"),
                    rr.get("target_horizon"),
                    score.get("gates_failed") or [],
                    score.get("gates_unknown") or [],
                    json.dumps(score, default=str),
                ],
            )
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
            db.execute(
                "UPDATE decision_scores SET board_action = %s, "
                "board_confidence = %s WHERE cycle_id = %s AND ticker = %s",
                [action, confidence, cycle_id, ticker.strip().upper()],
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("[DecisionScore] %s/%s: could not attach board "
                       "decision: %s: %s", cycle_id, ticker,
                       type(e).__name__, e)
