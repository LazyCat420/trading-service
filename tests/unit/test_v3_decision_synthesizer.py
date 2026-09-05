import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from app.v3.shared_desk import SharedDesk, DeskPhase
from app.v3.artifacts import validate_artifact
from app.services.trade_result_saver import save_trade_result
from app.services.pipeline_service import PipelineService

def test_trade_decision_schema_validation():
    # Valid artifact
    valid_artifact = {
        "action": "BUY",
        "confidence": 75,
        "reasoning": "Strong support, low volatility regime.",
        "signal_weights": {"quant": 0.5, "fundamental": 0.5},
        "signal_assessments": {"quant": "Positive", "fundamental": "Undervalued"},
        "risk_flags": ["High debt"],
        "stop_loss": 100.0,
        "take_profit": 120.0,
        "position_size_pct": 5.0
    }
    errors = validate_artifact("trade_decision", valid_artifact)
    assert not errors, f"Should be valid: {errors}"

    # Invalid artifact — missing required field 'action'
    invalid_artifact = {
        "confidence": 75,
        "reasoning": "Missing action"
    }
    errors = validate_artifact("trade_decision", invalid_artifact)
    assert errors, "Should be invalid due to missing 'action'"

    # Invalid artifact — action is not a valid enum member
    invalid_action = {
        "action": "INVALID_ACTION_NAME",
        "confidence": 80,
        "reasoning": "Invalid enum action test"
    }
    errors = validate_artifact("trade_decision", invalid_action)
    assert errors, "Should be invalid due to invalid action enum"


def test_save_trade_result_database_logic():
    ticker = "AAPL"
    cycle_id = "test-cycle-123"
    verdict = {
        "action": "BUY",
        "confidence": 80,
        "reasoning": "Great setup",
        "signal_weights": {"quant": 0.4, "fundamental": 0.6},
        "signal_assessments": {"quant": "Bullish", "fundamental": "Neutral"},
        "risk_flags": [],
        "stop_loss": 150.0,
        "take_profit": 180.0,
        "position_size_pct": 3.0,
        "persona_used": "Quant-Heavy",
        "regime": "HIGH_VOLATILITY"
    }

    # Three writes since 2026-08-05: the delete/insert pair for the result
    # itself, then the deterministic-baseline pairing — which must be LAST
    # and must be a separate, non-fatal write, so a shadow write can neither
    # roll back nor block the decision it annotates. This used to be asserted
    # against SQL text and positional parameter tuples; it now reads the
    # collections, filters and documents, which is what actually determines
    # whether the right row is touched.
    from app.services import trade_result_saver as trs
    from app.quant import decision_score_store as dss

    saver_store = MagicMock()
    score_store = MagicMock()
    with patch.object(trs, "mongo_store", saver_store), \
         patch.object(dss, "mongo_store", score_store):
        save_trade_result(ticker, cycle_id, verdict)

    # Delete-then-insert on the same ticker+cycle: the upsert.
    del_collection, del_filter = saver_store.delete_docs.call_args[0][:2]
    assert del_collection == "trade_results"
    assert del_filter == {"ticker": ticker, "cycle_id": cycle_id}

    ins_collection, ins_docs = saver_store.insert_docs.call_args[0][:2]
    assert ins_collection == "trade_results"
    doc = ins_docs[0]
    assert doc["ticker"] == ticker
    assert doc["cycle_id"] == cycle_id
    assert doc["action"] == "BUY"
    assert doc["confidence"] == 80
    assert doc["reasoning"] == "Great setup"
    # Structured fields are stored as documents now, not serialized JSONB.
    assert doc["signal_weights"] == {"quant": 0.4, "fundamental": 0.6}
    assert doc["signal_assessments"] == {"quant": "Bullish", "fundamental": "Neutral"}
    assert doc["risk_flags"] == []

    # The baseline pairing is an UPDATE of an existing decision_scores row,
    # never an insert: a decision with no baseline row means the scorer did
    # not run for that desk, and creating one here would hide that.
    score_store.insert_docs.assert_not_called()
    upd_collection, upd_filter, upd_update = score_store.update_docs.call_args[0][:3]
    assert upd_collection == "decision_scores"
    assert upd_filter == {"cycle_id": cycle_id, "ticker": ticker}
    assert upd_update == {"$set": {"board_action": "BUY", "board_confidence": 80}}


@pytest.mark.asyncio
async def test_confidence_threshold_gating():
    # We will run PipelineService._run_all_v3 and mock upstream elements
    ticker = "AAPL"
    cycle_id = "test-cycle"
    
    with patch("app.services.pipeline_service.run_v3_pipeline") as mock_run_pipeline, \
         patch("app.services.result_saver.save_analysis_result") as mock_save_result, \
         patch("app.services.pipeline_state.PipelineStateDB.append_events") as mock_append_events, \
         patch("app.services.pipeline_state.PipelineStateDB.save_state") as mock_save_state, \
         patch("app.services.llm_preflight.llm_can_answer",
               new_callable=AsyncMock, return_value=(True, "unit test: probe mocked")), \
         patch("app.services.llm_preflight.tool_calls_are_parsed",
               new_callable=AsyncMock, return_value=(True, "unit test: tool probe mocked")), \
         patch("app.trading.paper_trader.buy", new_callable=AsyncMock) as mock_buy, \
         patch("app.trading.paper_trader.sell", new_callable=AsyncMock) as mock_sell:

        # Read the live floor rather than hardcoding it. This test is about the
        # BOUNDARY behaviour — at the floor trades, one below does not — and
        # pinning the literal 65 made it fail for the wrong reason when the floor
        # was re-fitted to 70 on measured evidence (2026-07-26).
        from app.services.parameter_store import get_param
        floor = get_param("ANALYSIS_CONFIDENCE_THRESHOLD")

        # 1. Exactly at the threshold -> should execute
        mock_run_pipeline.return_value = {
            "action": "BUY",
            "confidence": floor,
            "rationale": "Confidence at threshold",
            "estimate": {}
        }
        await PipelineService._run_all_v3(cycle_id, [ticker])
        mock_buy.assert_called_once()
        mock_sell.assert_not_called()

        mock_buy.reset_mock()
        mock_sell.reset_mock()

        # 2. One below the threshold -> should block
        mock_run_pipeline.return_value = {
            "action": "BUY",
            "confidence": floor - 1,
            "rationale": "Confidence below threshold",
            "estimate": {}
        }
        await PipelineService._run_all_v3(cycle_id, [ticker])
        mock_buy.assert_not_called()
        mock_sell.assert_not_called()

        mock_buy.reset_mock()
        mock_sell.reset_mock()

        # 3. Test None confidence -> Should block trade
        mock_run_pipeline.return_value = {
            "action": "BUY",
            "confidence": None,
            "rationale": "Confidence is None",
            "estimate": {}
        }
        await PipelineService._run_all_v3(cycle_id, [ticker])
        mock_buy.assert_not_called()
        mock_sell.assert_not_called()


@pytest.mark.asyncio
async def test_persist_trade_verdict_falls_back_to_board():
    """When decision_synthesizer returns None, _persist_trade_verdict must fall back
    to desk.final_decision (from Board of Directors) so the trade verdict is not dropped."""
    from app.v3.orchestrator import _persist_trade_verdict
    from app.v3.shared_desk import SharedDesk

    desk = SharedDesk(ticker="CRDO")
    desk.trade_decision = None
    desk.final_decision = {
        "action": "HOLD",
        "confidence": 55,
        "reasoning": "Oversold but confirmed downtrend below all SMAs.",
        "stop_loss": 123.52,
        "take_profit": 277.81,
        "position_size_pct": 0.0,
        "risk_flags": ["valuation_compression"],
    }

    saved_rows = []
    mock_mongo = MagicMock()
    mock_mongo.find_docs.return_value = []
    with patch("app.services.trade_result_saver.save_trade_result", side_effect=lambda tk, cid, dec: saved_rows.append(dec)), \
         patch("app.db.mongo_store.find_docs", return_value=[]), \
         patch("app.services.rlm_audit.log_rlm_audit_trail"), \
         patch("app.trading.strategy_tracker.record_strategy"):
        await _persist_trade_verdict(
            desk, None, cycle_id="test-fallback-cycle", bot_id="test-bot", ticker="CRDO", regime="NORMAL", source="unit_test"
        )

    assert len(saved_rows) == 1, "Should have saved 1 fallback trade result"
    saved = saved_rows[0]
    assert saved["action"] == "HOLD"
    assert saved["confidence"] == 55
    assert "Board of Directors" in saved["reasoning"]
    assert saved["signal_weights"] == {"quant": 0.25, "fundamental": 0.25, "debate": 0.25, "board": 0.25}
    assert saved["decision_provenance"] == "board_fallback"
    assert desk.trade_decision is not None
    assert desk.trade_decision["decision_provenance"] == "board_fallback"



