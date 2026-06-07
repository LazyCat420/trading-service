import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app.cycle.trading_phase import execute_decisions

@pytest.mark.asyncio
async def test_sell_non_held_assets_skipped_no_error():
    """Verify that a SELL decision for a ticker NOT held in the portfolio is skipped cleanly

    without triggering an error or incrementing buy/sell failed counts.
    """
    decisions = [
        {"ticker": "AAPL", "action": "SELL", "confidence": 90, "rationale": "Overvalued"},
    ]
    
    mock_portfolio = {
        "cash": 10000.0,
        "positions": [
            {"ticker": "MSFT", "qty": 100.0, "avg_entry_price": 300.0}
        ]
    }
    
    with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
         patch("app.cycle.trading_phase.sell") as mock_sell:
         
        res = await execute_decisions(decisions, bot_id="test-bot", cycle_id="test-cycle")
        
        # sell() should not be called since AAPL is not held
        mock_sell.assert_not_called()
        
        # Counts should show: holds=0, buy_executed=0, sell_executed=0, sell_skipped=1, buy_failed=0, sell_failed=0
        assert res["counts"]["sell_skipped"] == 1
        assert res["counts"]["sell_failed"] == 0
        assert len(res["skipped"]) == 1
        assert res["skipped"][0]["ticker"] == "AAPL"
        assert "No open position" in res["skipped"][0]["reason"]

@pytest.mark.asyncio
async def test_sell_held_assets_executed_successfully():
    """Verify that a SELL decision for a held asset is executed successfully."""
    decisions = [
        {"ticker": "AAPL", "action": "SELL", "confidence": 90, "rationale": "Take profit"},
    ]
    
    mock_portfolio = {
        "cash": 10000.0,
        "positions": [
            {"ticker": "AAPL", "qty": 50.0, "avg_entry_price": 150.0}
        ]
    }
    
    with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
         patch("app.cycle.trading_phase.sell", return_value={"status": "completed"}) as mock_sell:
         
        res = await execute_decisions(decisions, bot_id="test-bot", cycle_id="test-cycle")
        
        mock_sell.assert_called_once_with("test-bot", "AAPL", qty_pct=1.0, cycle_id="test-cycle")
        assert res["counts"]["sell_executed"] == 1
        assert res["counts"]["sell_failed"] == 0

@pytest.mark.asyncio
async def test_buy_positions_capped_at_max_capacity():
    """Verify that a BUY decision is blocked when active positions are at max capacity (8)."""
    decisions = [
        {"ticker": "NVDA", "action": "BUY", "confidence": 85, "rationale": "AI growth"},
    ]
    
    # Portfolio already has 8 active positions
    mock_portfolio = {
        "cash": 5000.0,
        "positions": [
            {"ticker": f"T{i}", "qty": 10.0, "avg_entry_price": 100.0} for i in range(8)
        ]
    }
    
    with patch("app.cycle.trading_phase.get_portfolio", return_value=mock_portfolio), \
         patch("app.cycle.trading_phase.buy") as mock_buy:
         
        res = await execute_decisions(decisions, bot_id="test-bot", cycle_id="test-cycle")
        
        # buy() should not be called since we are at max capacity
        mock_buy.assert_not_called()
        assert res["counts"]["blocked"] == 1
        assert len(res["skipped"]) == 1
        assert res["skipped"][0]["ticker"] == "NVDA"
        assert "max capacity" in res["skipped"][0]["reason"]
