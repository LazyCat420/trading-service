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
        # The last gate before the database. The runner normalises and stamps
        # provenance on the V3 path, but `save_trade_result` is reachable from
        # other callers, and an off-sum vector reached an EXECUTED order once
        # already (ZS, cycle-v3-1788646388). Re-running the pure function here
        # is idempotent for anything the runner already fixed.
        from app.v3.artifacts import (
            SIGNAL_WEIGHTS_SOURCES,
            normalize_signal_weights,
        )

        signal_weights, _derived_source = normalize_signal_weights(
            verdict.get("signal_weights")
        )
        # Prefer what the runner recorded: it saw the ORIGINAL vector, so it
        # can distinguish "the model said this" from "we rescaled it". By the
        # time the value reaches here it already sums to 1 and would re-derive
        # as `model`, erasing the repair.
        _declared = verdict.get("signal_weights_source")
        signal_weights_source = (
            _declared if _declared in SIGNAL_WEIGHTS_SOURCES else _derived_source
        )
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
        provenance = verdict.get("decision_provenance")
        if not isinstance(provenance, str) or not provenance.strip():
            provenance = None

        result_id = str(uuid.uuid4())
        _saved_at = datetime.now(timezone.utc)

        # Upsert in Mongo: remove existing for this ticker+cycle to avoid duplicates
        mongo_store.delete_docs('trade_results', {'ticker': ticker, 'cycle_id': cycle_id})
        mongo_store.insert_docs('trade_results', [{
            'id': result_id,
            'ticker': ticker,
            'cycle_id': cycle_id,
            'action': action,
            'confidence': confidence,
            'reasoning': reasoning[:2000] if reasoning else "",
            'signal_weights': signal_weights,
            'signal_weights_source': signal_weights_source,
            'signal_assessments': signal_assessments,
            'risk_flags': risk_flags,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'position_size_pct': position_size_pct,
            'persona_used': persona_used,
            'regime': regime,
            'internal_consensus_score': consensus,
            'dynamic_trigger': dynamic_trigger,
            'decision_provenance': provenance,
            'created_at': _saved_at,
        }])

        try:
            from app.quant.decision_score_store import attach_board_decision
            attach_board_decision(cycle_id, ticker, action, confidence)
        except Exception as e:
            logger.debug("[TradeResultSaver] %s/%s: baseline pairing skipped (non-fatal): %s", cycle_id, ticker, e)

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
