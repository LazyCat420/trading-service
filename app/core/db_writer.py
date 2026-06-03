"""
DB Writer — shared database write helpers.

Extracted from decision_engine.py so that runner.py and ticker_pipeline
don't need to import a 1279-line file for 2 small functions.

Functions:
  - log_decision():         Write analysis result to analysis_results table
  - execute_quarantine():   Quarantine a ticker (insufficient data / fake)

IMPORTANT: The SQL here must exactly match the real analysis_results table
schema.  The table columns are:
  id, cycle_id, bot_id, ticker, agent_name, result_json, confidence,
  created_at, triage_tier, thesis_verdict, thesis_confidence,
  thesis_summary, thesis_updated_at, thesis_unchanged
"""

import json
import logging
import uuid
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
    """Persist an analysis result to the analysis_results + cycle_summaries tables.

    This is a direct extraction of decision_engine.py._log_decision().
    The SQL, column names, and payload structure are identical so the
    frontend, trading phase, and report service all work unchanged.

    Both INSERTs are wrapped in a single transaction for atomicity.
    Uses ON CONFLICT upsert to prevent duplicates.
    """
    try:
        from app.utils.text_utils import sanitize_surrogates

        result = sanitize_surrogates(result)
        ticker = result["ticker"]

        with get_db() as db:
            result_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_OID,
                    f"{cycle_id}_{bot_id}_{ticker}_{result.get('config_used', 'C')}",
                )
            )

            # Compute estimate inline for BUY actions so it persists in DB
            estimate = None
            action = result.get("action", "HOLD")
            confidence = result.get("confidence", 0)

            if action == "BUY" and confidence > 0:
                try:
                    from app.cycle.trading_phase import estimate_trade
                    from app.trading.paper_trader import get_portfolio

                    pf = get_portfolio(bot_id or "default")
                    cash = pf.get("cash", 0)
                    price_row = db.execute(
                        "SELECT close FROM price_history WHERE ticker = %s ORDER BY date DESC LIMIT 1",
                        [ticker],
                    ).fetchone()
                    if price_row and price_row[0] > 0:
                        estimate = estimate_trade(confidence, cash, price_row[0])
                except Exception as est_err:
                    logger.warning(
                        "[DB] Estimate calc failed for %s: %s", ticker, est_err
                    )

            # Build the full result payload for the frontend
            result_payload = {
                "action": action,
                "confidence": confidence,
                "rationale": result.get("rationale", ""),
                "config_used": result.get("config_used", ""),
                "escalated": result.get("escalated", False),
                "human_review": result.get("human_review", False),
                "agent_tokens": result.get("agent_tokens", 0),
                "rlm_tokens": result.get("rlm_tokens", 0),
                "total_tokens": result.get("total_tokens", 0),
                "total_time_s": result.get("total_time_s"),
                "agent_results": result.get("agent_results", {}),
                "c_result": {
                    "action": result.get("c_result", {}).get("action"),
                    "confidence": result.get("c_result", {}).get("confidence"),
                }
                if result.get("c_result")
                else None,
                "d_result": {
                    "action": result.get("d_result", {}).get("action"),
                    "confidence": result.get("d_result", {}).get("confidence"),
                    "original_thesis_status": result.get("d_result", {}).get(
                        "original_thesis_status", "NOT_HELD"
                    ),
                    "original_thesis_explanation": result.get("d_result", {}).get(
                        "original_thesis_explanation", ""
                    ),
                }
                if result.get("d_result")
                else None,
            }

            if estimate:
                result_payload["estimate"] = estimate

            # Determine if this run should save thesis state
            _is_thesis_run = result.get("triage_tier") in ("standard", "deep")
            _thesis_now = datetime.now(timezone.utc) if _is_thesis_run else None

            # Wrap both INSERTs in a transaction for atomicity
            with db.transaction():
                # Upsert on (id) to prevent duplicate rows
                db.execute(
                    """
                    INSERT INTO analysis_results
                    (id, cycle_id, bot_id, ticker, agent_name, result_json, confidence, created_at, triage_tier,
                     thesis_verdict, thesis_confidence, thesis_summary, thesis_updated_at, thesis_unchanged)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                    ON CONFLICT (id) DO NOTHING
                """,
                    [
                        result_id,
                        cycle_id or "manual",
                        bot_id or "decision-engine",
                        ticker,
                        f"hybrid_{result.get('config_used', 'C')}",
                        json.dumps(result_payload),
                        confidence,
                        result.get("timestamp"),
                        result.get("triage_tier", "standard"),
                        # Thesis fields — only populated for standard/deep runs
                        action if _is_thesis_run else None,
                        confidence if _is_thesis_run else None,
                        result.get("rationale", "")[:1500] if _is_thesis_run else None,
                        _thesis_now,
                    ],
                )

                # Upsert cycle_summaries to prevent PK violation on (ticker, cycle_id)
                db.execute(
                    """
                    INSERT INTO cycle_summaries
                    (ticker, cycle_id, cycle_date, agent_name, action, confidence, confidence_tier, rationale_summary, was_correct, outcome_pnl)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ticker, cycle_id) DO UPDATE SET
                        action = EXCLUDED.action,
                        confidence = EXCLUDED.confidence,
                        confidence_tier = EXCLUDED.confidence_tier,
                        rationale_summary = EXCLUDED.rationale_summary,
                        agent_name = EXCLUDED.agent_name
                    """,
                    [
                        ticker,
                        cycle_id or "manual",
                        result.get("timestamp"),
                        f"hybrid_{result.get('config_used', 'C')}",
                        action,
                        confidence,
                        "high"
                        if confidence >= 70
                        else "medium"
                        if confidence >= 40
                        else "low",
                        result.get("rationale", "")[:500],
                        None,  # was_correct
                        None,  # outcome_pnl
                    ],
                )

        # Record BUY/SELL decisions for outcome tracking
        if action in ("BUY", "SELL"):
            try:
                from app.pipeline.analysis.outcome_tracker import record_decision

                _entry_price = None
                if estimate:
                    _entry_price = estimate.get("price")
                record_decision(
                    cycle_id=cycle_id or "manual",
                    ticker=ticker,
                    action=action,
                    confidence=confidence,
                    entry_price=_entry_price,
                    lesson=result.get("rationale", "")[:200],
                )
            except Exception as outcome_err:
                logger.warning(
                    "[DB] record_decision failed for %s: %s", ticker, outcome_err
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
