"""
Trade Result Saver — Persists structured trade verdicts to the trade_results table.

Called by the pipeline after the Decision Synthesizer (Layer 5) produces a verdict.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from app.db import mongo_store

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

        # Reject an unparseable action AT THE WRITE, not only at the policy gate.
        # Three rows reached this table with actions no consumer understands:
        # two `NEUTRAL` and one literal `BUY|SELL|HOLD` (the model echoing the
        # schema's enum instead of choosing from it). Every downstream reader
        # tests `action IN ('BUY','SELL')`, so these rows are silently invisible
        # to execution and silently COUNTED by accuracy queries — laundering a
        # parse failure into a decision. Storing HOLD is honest: no executable
        # action was expressed, and the original is preserved in the reasoning.
        _raw_action = verdict.get("action", "HOLD")
        action = str(_raw_action or "HOLD").strip().upper()
        if action not in ("BUY", "SELL", "HOLD"):
            logger.warning(
                "[TradeResultSaver] %s/%s: unparseable action %r — storing HOLD "
                "rather than a value no consumer understands",
                cycle_id, ticker, _raw_action,
            )
            action = "HOLD"
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
        consensus = verdict.get("internal_consensus_score")
        if not isinstance(consensus, (int, float)) or isinstance(consensus, bool):
            consensus = None
        else:
            consensus = int(consensus)
        dynamic_trigger = verdict.get("dynamic_trigger")
        if not isinstance(dynamic_trigger, dict):
            dynamic_trigger = None
        # Where this action came from. Absent only on artifacts written before
        # 2026-07-25; a missing value is "unknown", NOT "an agent decided it",
        # so it is stored as NULL rather than defaulting to board_reasoned.
        provenance = verdict.get("decision_provenance")
        if not isinstance(provenance, str) or not provenance.strip():
            provenance = None

        result_id = str(uuid.uuid4())

        # ONE timestamp for both stores — a second now() at mirror time gave
        # every live-mirrored row a created_at a few ms off its PG twin, which
        # field-level parity verification (2026-08-16) flagged on 99/200 rows.
        _saved_at = datetime.now(timezone.utc)
        with get_db() as db:
            with db.transaction():
                # Upsert: remove existing for this ticker+cycle to avoid duplicates
                mongo_store.delete_docs('trade_results', {'ticker': ticker, 'cycle_id': cycle_id})

                mongo_store.insert_docs('trade_results', [{'id': result_id, 'ticker': ticker, 'cycle_id': cycle_id, 'action': action, 'confidence': confidence, 'reasoning': reasoning[:2000] if reasoning else "", 'signal_weights': json.dumps(signal_weights), 'signal_assessments': json.dumps(signal_assessments), 'risk_flags': json.dumps(risk_flags), 'stop_loss': stop_loss, 'take_profit': take_profit, 'position_size_pct': position_size_pct, 'persona_used': persona_used, 'regime': regime, 'internal_consensus_score': consensus, 'dynamic_trigger': json.dumps(dynamic_trigger) if dynamic_trigger else None, 'decision_provenance': provenance, 'created_at': _saved_at}])

        # Pair the agents' verdict with the deterministic baseline recorded at
        # desk-build time (2026-08-05). Shadow only — nothing reads it back
        # into a decision; it exists so "did the computed score rank better
        # than the board's confidence?" is a query rather than a rebuild. It
        # cannot be reconstructed later: `fundamentals` is overwritten as
        # vendors refresh, so a score recomputed next month is not the score
        # this desk saw.
        try:
            from app.quant.decision_score_store import attach_board_decision
            attach_board_decision(cycle_id, ticker, action, confidence)
        except Exception as e:
            logger.debug("[TradeResultSaver] %s/%s: baseline pairing skipped "
                         "(non-fatal): %s", cycle_id, ticker, e)

        # Best-effort Mongo mirror — replace by (cycle_id, ticker) to match the
        # PG delete-first upsert (JSONB fields stored as native dicts).
        try:
            from app.db import mongo_store
            if mongo_store.writes_mongo("trade_results"):
                mongo_store.upsert_doc("trade_results", {"cycle_id": cycle_id, "ticker": ticker}, {
                    "id": result_id, "ticker": ticker, "cycle_id": cycle_id, "action": action,
                    "confidence": confidence, "reasoning": reasoning[:2000] if reasoning else "",
                    "signal_weights": signal_weights, "signal_assessments": signal_assessments,
                    "risk_flags": risk_flags, "stop_loss": stop_loss, "take_profit": take_profit,
                    "position_size_pct": position_size_pct, "persona_used": persona_used,
                    "regime": regime, "internal_consensus_score": consensus,
                    "dynamic_trigger": dynamic_trigger,
                    "decision_provenance": provenance,
                    "created_at": _saved_at,
                })
        except Exception:
            pass

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
