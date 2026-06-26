import pytest
from unittest.mock import patch, MagicMock
from app.v3.orchestrator import _build_cycle_metadata, _extract_debate_result
from app.services.trade_result_saver import save_trade_result
from app.v3.shared_desk import SharedDesk

def test_c1_portfolio_context_swallowed():
    """Verify that get_position_context exception does not crash but is logged properly."""
    with patch("app.v3.orchestrator.logger.warning") as mock_logger:
        
        metadata = _build_cycle_metadata("AAPL", "bot-123")
        
        # Function should not crash
        assert metadata["ticker"] == "AAPL"
        
        # Should log the warning because the internal import fails (ImportError)
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
    """Verify that string confidences are cast to int before comparison."""
    desk = SharedDesk("AAPL", "cycle-1")
    
    # Let's use something that breaks string comparison:
    # Lexicographically, "9" > "80" is TRUE!
    # Mathematically, 9 > 80 is FALSE!
    desk.bull_argument = {"confidence": "9"}
    desk.bear_rebuttal = {"confidence": "80"}
    desk.bull_defense = {"final_confidence": "9"}
    
    result = _extract_debate_result(desk)
    
    # The actual winner should be bear (80 > 9)
    assert result["winning_side"] == "bear"
    assert result["confidence"] == 80
