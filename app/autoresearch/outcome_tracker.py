"""
Outcome Tracker — Records pipeline decisions and resolves them against actual prices.

This closes the feedback loop for Decision Quality scoring:
1. record_cycle_decisions()  — called after each cycle, captures BUY/SELL/HOLD + entry price
2. resolve_pending_outcomes() — called before scoring, checks unresolved decisions against current prices
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# How many days to wait before resolving a decision outcome
RESOLVE_AFTER_DAYS = 7
# PnL thresholds for WIN/LOSS/FLAT classification
WIN_THRESHOLD_PCT = 1.0
LOSS_THRESHOLD_PCT = -1.0


def record_cycle_decisions(cycle_id: str, cycle_summary: dict) -> int:
    """
    After a cycle completes, read analysis_results for that cycle
    and insert unresolved decision_outcomes for every BUY/SELL decision.
    HOLD decisions are skipped since they have no price action to evaluate.
    """
    recorded = 0
    try:
        with get_db() as db:
            rows = db.execute(
                """
                SELECT ar.ticker, ar.confidence,
                       COALESCE(
                           (SELECT ph.close FROM price_history ph
                            WHERE ph.ticker = ar.ticker ORDER BY ph.date DESC LIMIT 1),
                           NULL
                       ) AS entry_price,
                       ar.result_json
                FROM analysis_results ar
                WHERE ar.cycle_id = %s AND ar.confidence IS NOT NULL
                """,
                [cycle_id],
            ).fetchall()

            for ticker, confidence, entry_price, result_json in rows:
                # Extract action from result_json
                import json
                try:
                    result = json.loads(result_json) if isinstance(result_json, str) else (result_json or {})
                except (json.JSONDecodeError, TypeError):
                    result = {}

                action = result.get("action", "HOLD")
                if action == "HOLD":
                    continue  # Nothing to track for HOLD

                if entry_price is None:
                    logger.debug("[OUTCOME] Skipping %s — no price_history available", ticker)
                    continue

                # Check if we already recorded this cycle+ticker combo
                existing = db.execute(
                    "SELECT id FROM decision_outcomes WHERE cycle_id = %s AND ticker = %s",
                    [cycle_id, ticker],
                ).fetchone()
                if existing:
                    continue

                outcome_id = f"do-{uuid.uuid4().hex[:12]}"
                db.execute(
                    """INSERT INTO decision_outcomes
                    (id, cycle_id, ticker, action, confidence, entry_price, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    [outcome_id, cycle_id, ticker, action, confidence,
                     round(entry_price, 4), datetime.now(timezone.utc)],
                )
                recorded += 1

        if recorded > 0:
            logger.info("[OUTCOME] Recorded %d decision outcomes for cycle %s", recorded, cycle_id[:12])
    except Exception as e:
        logger.error("[OUTCOME] Failed to record decisions: %s", e)

    return recorded


def resolve_pending_outcomes() -> dict:
    """
    Find unresolved decision_outcomes older than RESOLVE_AFTER_DAYS,
    look up current price, compute PnL, and classify as WIN/LOSS/FLAT.

    Returns summary stats.
    """
    resolved = 0
    errors = 0
    stats = {"wins": 0, "losses": 0, "flats": 0}

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RESOLVE_AFTER_DAYS)
        with get_db() as db:
            pending = db.execute(
                """
                SELECT id, ticker, action, entry_price, created_at
                FROM decision_outcomes
                WHERE resolved_at IS NULL AND created_at < %s
                ORDER BY created_at ASC
                LIMIT 50
                """,
                [cutoff],
            ).fetchall()

            for outcome_id, ticker, action, entry_price, created_at in pending:
                try:
                    # Get current price
                    price_row = db.execute(
                        "SELECT close FROM price_history WHERE ticker = %s ORDER BY date DESC LIMIT 1",
                        [ticker],
                    ).fetchone()

                    if not price_row or price_row[0] is None:
                        logger.debug("[OUTCOME] Cannot resolve %s — no current price for %s", outcome_id, ticker)
                        continue

                    exit_price = price_row[0]

                    if entry_price is None or entry_price == 0:
                        logger.debug("[OUTCOME] Cannot resolve %s — invalid entry_price", outcome_id)
                        continue

                    # Compute PnL based on action direction
                    if action == "BUY":
                        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    elif action == "SELL":
                        pnl_pct = ((entry_price - exit_price) / entry_price) * 100
                    else:
                        continue  # Shouldn't happen — HOLDs aren't recorded

                    # Classify outcome
                    if pnl_pct >= WIN_THRESHOLD_PCT:
                        outcome = "WIN"
                        stats["wins"] += 1
                    elif pnl_pct <= LOSS_THRESHOLD_PCT:
                        outcome = "LOSS"
                        stats["losses"] += 1
                    else:
                        outcome = "FLAT"
                        stats["flats"] += 1

                    db.execute(
                        """UPDATE decision_outcomes
                        SET exit_price = %s, pnl_pct = %s, outcome = %s, resolved_at = %s
                        WHERE id = %s""",
                        [round(exit_price, 4), round(pnl_pct, 2), outcome,
                         datetime.now(timezone.utc), outcome_id],
                    )
                    resolved += 1

                except Exception as row_err:
                    errors += 1
                    logger.warning("[OUTCOME] Failed to resolve %s: %s", outcome_id, row_err)

        if resolved > 0:
            logger.info(
                "[OUTCOME] Resolved %d outcomes: %dW / %dL / %dF (errors: %d)",
                resolved, stats["wins"], stats["losses"], stats["flats"], errors,
            )
    except Exception as e:
        logger.error("[OUTCOME] Batch resolution failed: %s", e)

    return {"resolved": resolved, "errors": errors, **stats}
