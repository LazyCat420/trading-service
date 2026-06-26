"""
Trade Result Saver — Persists structured trade verdicts to the trade_results table.

Called by the pipeline after the Decision Synthesizer (Layer 5) produces a verdict.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def save_trade_result(ticker: str, cycle_id: str, verdict: dict) -> None:
    """Persist a trade verdict to the trade_results table.

    Args:
        ticker: Stock ticker symbol.
        cycle_id: Current pipeline cycle ID.
        verdict: Decision synthesizer output dict with action, confidence, etc.
    """
    try:
        from app.db.connection import get_db

        action = verdict.get("action", "HOLD")
        confidence = int(verdict.get("confidence", 0))
        reasoning = verdict.get("reasoning", "")
        signal_weights = verdict.get("signal_weights", {})
        signal_assessments = verdict.get("signal_assessments", {})
        risk_flags = verdict.get("risk_flags", [])
        stop_loss = verdict.get("stop_loss")
        take_profit = verdict.get("take_profit")
        position_size_pct = verdict.get("position_size_pct")
        persona_used = verdict.get("persona_used", "")
        regime = verdict.get("regime", "")

        result_id = str(uuid.uuid4())

        with get_db() as db:
            with db.transaction():
                # Upsert: remove existing for this ticker+cycle to avoid duplicates
                db.execute(
                    "DELETE FROM trade_results WHERE ticker = %s AND cycle_id = %s",
                    [ticker, cycle_id],
                )

                db.execute(
                    """
                    INSERT INTO trade_results (
                        id, ticker, cycle_id, action, confidence,
                        reasoning, signal_weights, signal_assessments,
                        risk_flags, stop_loss, take_profit,
                        position_size_pct, persona_used, regime,
                        created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s
                    )
                    """,
                    [
                        result_id,
                        ticker,
                        cycle_id,
                        action,
                        confidence,
                        reasoning[:2000] if reasoning else "",
                        json.dumps(signal_weights),
                        json.dumps(signal_assessments),
                        json.dumps(risk_flags),
                        stop_loss,
                        take_profit,
                        position_size_pct,
                        persona_used,
                        regime,
                        datetime.now(timezone.utc),
                    ],
                )

        logger.info(
            "[trade_result_saver] Saved trade result for %s: %s @ %d%% (cycle: %s)",
            ticker,
            action,
            confidence,
            cycle_id,
        )
    except Exception as e:
        logger.error(
            "[trade_result_saver] Failed to save trade result for %s: %s",
            ticker,
            e,
        )
        raise
