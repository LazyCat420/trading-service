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

    with patch("app.db.connection.get_db") as mock_get_db:
        mock_conn = MagicMock()
        mock_transaction = MagicMock()
        mock_conn.transaction.return_value = mock_transaction
        mock_get_db.return_value.__enter__.return_value = mock_conn

        save_trade_result(ticker, cycle_id, verdict)

        # Ensure transaction was used
        mock_conn.transaction.assert_called_once()
        mock_transaction.__enter__.assert_called_once()

        # Check executing queries
        calls = mock_conn.execute.call_args_list
        assert len(calls) == 2

        # First call is DELETE
        delete_query, delete_args = calls[0][0]
        assert "DELETE FROM trade_results" in delete_query
        assert delete_args == [ticker, cycle_id]

        # Second call is INSERT
        insert_query, insert_args = calls[1][0]
        assert "INSERT INTO trade_results" in insert_query
        assert insert_args[1] == ticker
        assert insert_args[2] == cycle_id
        assert insert_args[3] == "BUY"
        assert insert_args[4] == 80
        assert insert_args[5] == "Great setup"
        # Check serialized JSONB strings
        assert json.loads(insert_args[6]) == {"quant": 0.4, "fundamental": 0.6}
        assert json.loads(insert_args[7]) == {"quant": "Bullish", "fundamental": "Neutral"}
        assert json.loads(insert_args[8]) == []


@pytest.mark.asyncio
async def test_confidence_threshold_gating():
    # We will run PipelineService._run_all_v3 and mock upstream elements
    ticker = "AAPL"
    cycle_id = "test-cycle"
    
    with patch("app.services.pipeline_service.run_v3_pipeline") as mock_run_pipeline, \
         patch("app.services.result_saver.save_analysis_result") as mock_save_result, \
         patch("app.services.pipeline_state.PipelineStateDB.append_events") as mock_append_events, \
         patch("app.services.pipeline_state.PipelineStateDB.save_state") as mock_save_state, \
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


