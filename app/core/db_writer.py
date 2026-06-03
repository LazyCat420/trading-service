"""
DB Writer — shared database write helpers.

Extracted from decision_engine.py so that runner.py and ticker_pipeline
don't need to import a 1279-line file for 2 small functions.

Functions:
  - log_decision():         Write analysis result to analysis_results table
  - execute_quarantine():   Quarantine a ticker (insufficient data / fake)
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from app.db.connection import get_db
from app.log_manager import log_manager

logger = logging.getLogger(__name__)


def log_decision(
    result: dict[str, Any],
    cycle_id: str,
    bot_id: str,
) -> None:
    """Persist an analysis result to the analysis_results table.

    This is the single place where ticker decisions are written to the DB.
    Previously duplicated inside decision_engine.py._log_decision().
    """
    ticker = result.get("ticker", "?")
    action = result.get("action", "HOLD")
    confidence = result.get("confidence", 0)
    rationale = result.get("rationale", "")
    config_used = result.get("config_used", "unknown")
    total_tokens = result.get("total_tokens", 0)
    total_time_s = result.get("total_time_s", 0)
    v2_metadata = result.get("v2_metadata")
    escalated = result.get("escalated", False)
    triage_tier = result.get("triage_tier", "standard")

    try:
        import json

        with get_db() as db:
            db.execute(
                """
                INSERT INTO analysis_results
                    (cycle_id, bot_id, ticker, action, confidence, rationale,
                     config_used, total_tokens, total_time_s, v2_metadata,
                     escalated, triage_tier, created_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cycle_id, ticker)
                DO UPDATE SET
                    action = EXCLUDED.action,
                    confidence = EXCLUDED.confidence,
                    rationale = EXCLUDED.rationale,
                    config_used = EXCLUDED.config_used,
                    total_tokens = EXCLUDED.total_tokens,
                    total_time_s = EXCLUDED.total_time_s,
                    v2_metadata = EXCLUDED.v2_metadata,
                    escalated = EXCLUDED.escalated,
                    triage_tier = EXCLUDED.triage_tier,
                    created_at = EXCLUDED.created_at
                """,
                [
                    cycle_id, bot_id, ticker, action, confidence,
                    rationale[:5000],  # Truncate rationale to prevent DB bloat
                    config_used, total_tokens, total_time_s,
                    json.dumps(v2_metadata) if v2_metadata else None,
                    escalated, triage_tier,
                    datetime.now(timezone.utc),
                ],
            )
        logger.info("[DB] Logged decision for %s: %s@%d%%", ticker, action, confidence)
    except Exception as e:
        logger.error("[DB] Failed to log decision for %s: %s", ticker, e)


def execute_quarantine(
    ticker: str,
    reason: str,
    cycle_id: str,
    bot_id: str,
    triage_tier: str = "standard",
    held: bool = False,
    emit: Callable | None = None,
    source: str = "data_sufficiency_gate",
) -> dict[str, Any]:
    """Quarantine a ticker — mark it as rejected and return a HOLD result.

    Used when a ticker has insufficient data, is fake/delisted, or fails
    the sufficiency gate. Previously in decision_engine.py._execute_quarantine().
    """
    from app.cognition.debate.action_gate import gate_action

    action = gate_action("HOLD", held)
    result = {
        "ticker": ticker,
        "action": action,
        "confidence": 0,
        "rationale": f"QUARANTINED ({source}): {reason}",
        "config_used": "quarantine",
        "triage_tier": triage_tier,
        "escalated": False,
        "agent_results": {},
        "c_result": {"action": action, "confidence": 0, "rationale": reason},
        "d_result": None,
        "human_review": False,
        "agent_tokens": 0,
        "rlm_tokens": 0,
        "total_tokens": 0,
        "total_time_s": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "is_timeout_fallback": True,
        "error": reason,
        "error_type": "quarantine",
    }

    # Log quarantine
    log_decision(result, cycle_id, bot_id)

    if emit:
        emit(
            "analyzing",
            f"quarantine_{ticker}",
            f"🚫 {ticker}: QUARANTINED — {reason}",
            status="warning",
        )

    logger.warning("[QUARANTINE] %s: %s (source=%s)", ticker, reason, source)

    # Remove from watchlist if fake
    if "price" in reason.lower() or "fake" in reason.lower() or "delisted" in reason.lower():
        try:
            with get_db() as db:
                db.execute("DELETE FROM watchlist WHERE ticker = %s", [ticker])
            logger.info("[QUARANTINE] Removed %s from watchlist", ticker)
        except Exception as e:
            logger.warning("[QUARANTINE] Failed to remove %s from watchlist: %s", ticker, e)

    return result
