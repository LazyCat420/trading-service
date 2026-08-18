import pytest
from unittest.mock import patch, MagicMock
from app.v3.orchestrator import _build_cycle_metadata, _extract_debate_result
from app.services.trade_result_saver import save_trade_result
from app.v3.shared_desk import SharedDesk

def test_c1_portfolio_context_swallowed():
    """Verify that get_position_context exception does not crash but is logged properly."""
    with patch("app.v3.orchestrator.logger.warning") as mock_logger, \
         patch("app.tools.portfolio_tools.get_position_context",
               side_effect=RuntimeError("DB unavailable")):

        metadata = _build_cycle_metadata("AAPL", "bot-123")

        # Function should not crash, and no portfolio context is attached
        assert metadata["ticker"] == "AAPL"
        assert "portfolio_context" not in metadata

        # Should log the warning from the swallowed exception
        assert mock_logger.call_count == 1
        call_args = mock_logger.call_args[0]
        assert "[V3] %s: Failed to fetch portfolio context: %s" in call_args[0]
        assert call_args[1] == "AAPL"

def test_c3_trade_result_double_swallowed():
    """Verify that save_trade_result raises exceptions to the caller."""
    ticker = "AAPL"
    cycle_id = "test-cycle"
    verdict = {"action": "BUY"}
    
    with patch("app.db.connection.get_db") as mock_get_db:
        mock_conn = MagicMock()
        mock_get_db.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.side_effect = Exception("DB DEAD")
        
        with pytest.raises(Exception, match="DB DEAD"):
            save_trade_result(ticker, cycle_id, verdict)

def test_c7_debate_confidence_logic_bomb():
    """Verify that string confidences are cast to int, never compared lexicographically.

    Winner selection now comes from the debate judge agent, but confidences
    still arrive as strings from LLM output and must be safely cast
    (lexicographically "9" > "80", mathematically 9 < 80).
    """
    desk = SharedDesk("AAPL", "cycle-1")
    desk.bull_argument = {"confidence": "9"}
    desk.bear_rebuttal = {"confidence": "80"}
    desk.debate_judge = {"winner": "bear", "final_confidence": "80", "summary": "bear case stronger"}

    result = _extract_debate_result(desk)

    assert result["winning_side"] == "bear"
    assert result["confidence"] == 80  # int, cast from "80"
    assert result["bull_confidence"] == 9
    assert result["bear_confidence"] == 80
    assert result["action"] == "SELL"
    assert result["original_thesis_status"] == "BROKEN"


def test_c7_missing_judge_falls_back_to_tie():
    """Without a debate_judge artifact the result degrades to a HOLD tie."""
    desk = SharedDesk("AAPL", "cycle-1")
    desk.bull_argument = {"confidence": "60"}
    desk.bear_rebuttal = {"confidence": "40"}

    result = _extract_debate_result(desk)

    assert result["winning_side"] == "tie"
    assert result["action"] == "HOLD"
    assert result["confidence"] == 0
