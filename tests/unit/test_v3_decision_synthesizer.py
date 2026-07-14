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

        # 1. Test exactly at threshold (65%) -> Should execute trade
        mock_run_pipeline.return_value = {
            "action": "BUY",
            "confidence": 65,
            "rationale": "Confidence at threshold",
            "estimate": {}
        }
        await PipelineService._run_all_v3(cycle_id, [ticker])
        mock_buy.assert_called_once()
        mock_sell.assert_not_called()

        mock_buy.reset_mock()
        mock_sell.reset_mock()

        # 2. Test below threshold (64%) -> Should block trade
        mock_run_pipeline.return_value = {
            "action": "BUY",
            "confidence": 64,
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


@pytest.mark.skip(reason="asserts the pre-June linear pipeline; needs rewrite for the event-driven orchestrator (artifact-queue phases)")
@pytest.mark.asyncio
async def test_orchestrator_layer5_integration():
    from app.v3.orchestrator import run_v3_pipeline
    
    # We will simulate running pipeline with mocks
    with patch("app.v3.orchestrator._run_agent_with_circuit_breaker", new_callable=AsyncMock) as mock_run_agent, \
         patch("app.v3.orchestrator.save_desk") as mock_save_desk, \
         patch("app.v3.orchestrator._run_board_of_directors", new_callable=AsyncMock) as mock_run_board, \
         patch("app.v3.orchestrator._build_v1_compatible_result") as mock_build_result, \
         patch("app.services.trade_result_saver.save_trade_result") as mock_save_trade_result:

        mock_run_agent.return_value = "SUCCESS"
        mock_run_board.return_value = "SUCCESS"
        
        # When decision agent is enabled
        with patch("app.config.settings.DECISION_AGENT_ENABLED", True):
            # Simulate agent adding trade_decision artifact
            def side_effect(desk, agent_module, **kwargs):
                desk.trade_decision = {
                    "action": "BUY",
                    "confidence": 85,
                    "reasoning": "Consensus"
                }
                return "SUCCESS"
            mock_run_agent.side_effect = side_effect
            
            await run_v3_pipeline(ticker="AAPL", cycle_id="test-cycle")
            
            # Assert decision agent was executed
            from app.v3.agents import decision_agent
            
            # Verify one of the run agent calls was for decision_agent
            found = False
            for call in mock_run_agent.call_args_list:
                if call[1].get("agent_module") == decision_agent:
                    found = True
            assert found, "decision_agent module was not executed in pipeline"
            mock_save_trade_result.assert_called_once()

        mock_save_trade_result.reset_mock()
        mock_run_agent.reset_mock()

        # When decision agent is disabled
        with patch("app.config.settings.DECISION_AGENT_ENABLED", False):
            await run_v3_pipeline(ticker="AAPL", cycle_id="test-cycle")
            # Should not call run_agent for decision synthesizer
            for call in mock_run_agent.call_args_list:
                assert call[1].get("agent_module") != decision_agent
            mock_save_trade_result.assert_not_called()
