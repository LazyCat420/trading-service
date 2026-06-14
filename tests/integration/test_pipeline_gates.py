# tests/integration/test_pipeline_gates.py

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.pipeline.analysis.decision_engine import analyze_ticker
from app.data.market_snapshot import MarketSnapshot
import datetime

@pytest.mark.asyncio
async def test_analyze_ticker_gated_by_market_data():
    """
    Assert that analyze_ticker intercepts empty/missing market data and routes to quarantine.
    """
    # Mock check_and_fill to simulate successful return
    mock_fill = AsyncMock(return_value={"filled": []})
    
    # Mock check_data_sufficiency to pass structural check
    mock_sufficiency = MagicMock(return_value={"sufficient": True, "gaps": []})
    
    # Mock get_latest_snapshot to return a snapshot missing price and volume
    dummy_snapshot = MarketSnapshot(
        ticker="BADT",
        fetched_at=datetime.datetime.now(datetime.UTC),
        data_source="mock",
        candles_used=0,
        price=None,     # Missing!
        open=10.0,
        high=11.0,
        low=9.0,
        volume=None,    # Missing!
        vwap=10.0,
        rsi_14=50.0,
        macd=0.0,
        macd_signal=0.0,
        macd_hist=0.0,
        bb_upper=12.0,
        bb_lower=8.0,
        bb_pct=50.0,
        sma_20=10.0,
        sma_50=10.0,
        sma_200=10.0,
        atr_14=1.0,
        adx_14=20.0,
        stoch_k=50.0,
        stoch_d=50.0,
        returns_1d=0.0,
        returns_5d=0.0,
        returns_20d=0.0,
        volatility_20d=0.1,
        sharpe_20d=1.0,
        max_drawdown_20d=0.0,
        beta_20d=1.0,
        pe_ratio=None,
        forward_pe=None,
        eps=None,
        market_cap=None,
        revenue_growth=None,
        profit_margin=None,
        debt_to_equity=None
    )
    
    # Mock get_latest_snapshot to return our bad snapshot
    mock_snapshot = MagicMock(return_value=dummy_snapshot)
    
    # Mock specialist agents run so it doesn't fail if reached (though it shouldn't be reached)
    mock_run_agents = AsyncMock(return_value={})
    
    with patch("app.pipeline.data.data_completeness.check_and_fill", mock_fill), \
         patch("app.pipeline.data.data_completeness.check_data_sufficiency", mock_sufficiency), \
         patch("app.data.market_data_store.get_latest_snapshot", mock_snapshot), \
         patch("app.pipeline.analysis.decision_engine._run_agents", mock_run_agents):
         
        # Run analyze_ticker
        result = await analyze_ticker("BADT", cycle_id="test_cycle", bot_id="test_bot")
        
        # Verify it went to quarantine and returned a synthetic remapped SELL
        assert result["config_used"] == "quarantine"
        assert result["action"] == "SELL"
        assert "quarantine" in result["rationale"].lower()
        assert "missing: ['price', 'volume']" in result["rationale"].lower()
