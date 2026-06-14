# tests/regression/test_blank_json_forwarding.py

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.pipeline.analysis.decision_engine import analyze_ticker
from app.data.market_snapshot import MarketSnapshot
import datetime

@pytest.mark.asyncio
async def test_blank_json_from_config_c_aborts_pipeline():
    """
    Regression test: Verifies that if Config C returns empty JSON or DATA_MISSING,
    the pipeline aborts immediately to prevent blank forwarding and does not run debate/Config D.
    """
    mock_fill = AsyncMock(return_value={"filled": []})
    mock_sufficiency = MagicMock(return_value={"sufficient": True, "gaps": []})
    
    # Valid market data to pass the initial gate
    dummy_snapshot = MarketSnapshot(
        ticker="REGRESS",
        fetched_at=datetime.datetime.now(datetime.UTC),
        data_source="mock",
        candles_used=0,
        price=100.0,
        open=100.0,
        high=105.0,
        low=95.0,
        volume=100000,
        vwap=100.0,
        rsi_14=50.0,
        macd=0.0,
        macd_signal=0.0,
        macd_hist=0.0,
        bb_upper=120.0,
        bb_lower=80.0,
        bb_pct=50.0,
        sma_20=100.0,
        sma_50=100.0,
        sma_200=100.0,
        atr_14=1.0,
        adx_14=20.0,
        stoch_k=50.0,
        stoch_d=50.0,
        returns_1d=0.0,
        returns_5d=0.0,
        returns_20d=0.1,
        volatility_20d=0.1,
        sharpe_20d=1.0,
        max_drawdown_20d=0.0,
        beta_20d=1.0,
        pe_ratio=15.0,
        forward_pe=14.0,
        eps=6.5,
        market_cap=10000000,
        revenue_growth=0.1,
        profit_margin=0.15,
        debt_to_equity=0.5
    )
    mock_snapshot = MagicMock(return_value=dummy_snapshot)
    
    # Specialist agents return successfully
    mock_run_agents = AsyncMock(return_value={"planner": {}, "retriever": {}, "verifier": {}, "synthesizer": {}})
    
    # Mock context builder to return instantly
    mock_build_context = AsyncMock(return_value="dummy context")

    # Mock RLM Config C to return a DATA_MISSING status or blank dictionary
    mock_rlm_analyze = AsyncMock(return_value={
        "action": "HOLD",
        "confidence": 0,
        "rationale": "DATA_MISSING: missing financial reports",
        "status": "DATA_MISSING",
        "proceed": False,
        "missing_fields": ["financials"]
    })
    
    # Spy on run_debate to ensure it is NEVER called
    mock_run_debate = AsyncMock()

    with patch("app.pipeline.data.data_completeness.check_and_fill", mock_fill), \
         patch("app.pipeline.data.data_completeness.check_data_sufficiency", mock_sufficiency), \
         patch("app.data.market_data_store.get_latest_snapshot", mock_snapshot), \
         patch("app.pipeline.analysis.decision_engine._run_agents", mock_run_agents), \
         patch("app.pipeline.analysis.context_builder.build_context_blob", mock_build_context), \
         patch("app.pipeline.analysis.decision_engine.rlm_analyze", mock_rlm_analyze), \
         patch("app.pipeline.analysis.decision_engine.run_debate", mock_run_debate):
         
        # Run analyze_ticker
        result = await analyze_ticker("REGRESS", cycle_id="test_cycle", bot_id="test_bot")
        
        # Verify it went to quarantine and returned a synthetic remapped SELL due to gated Config C
        assert result["config_used"] == "quarantine"
        assert result["action"] == "SELL"
        assert "quarantine" in result["rationale"].lower()
        assert "config c gated" in result["rationale"].lower()
        
        # Assert that run_debate was never invoked
        mock_run_debate.assert_not_called()
