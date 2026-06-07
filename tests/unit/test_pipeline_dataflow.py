import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from app.core.result_builder import build_v1_compatible_result
from app.cycle.phases.phase4_analysis import run_phase4_analysis
from app.cycle.context import CycleContext

@pytest.mark.asyncio
async def test_thesis_timeout_fallback_in_analysis_phase():
    """Verify that when a ticker analysis times out or fails during thesis generation,
    it cleanly falls back to HOLD at 0% confidence with the timeout flag set.
    """
    ctx = CycleContext(
        cycle_id="test-cycle-timeout",
        tickers=["AAPL"],
        collect=True,
        analyze=True,
        trade=True,
    )

    async def mock_timeout_execute(*args, **kwargs):
        raise asyncio.TimeoutError("Thesis generation timed out after 2 attempts")

    # Patch execute_v2_pipeline to simulate a timeout/failure
    with patch("app.cognition.orchestration.runner.execute_v2_pipeline", side_effect=mock_timeout_execute), \
         patch("app.cycle.phases.phase4_analysis.settings") as mock_settings:
         
        mock_settings.V2_TICKER_CONCURRENCY = 1
        mock_settings.ANALYSIS_WORKER_TIMEOUT_SECONDS = 0.1
        
        cycle_summary = {"buy_count": 0, "sell_count": 0, "hold_count": 0, "review_count": 0}
        
        results = await run_phase4_analysis(
            ctx=ctx,
            bot_id="test-bot",
            macro_memo="mock macro",
            emit=lambda *args, **kwargs: None,
            cycle_summary=cycle_summary,
            state={"triage": {}},
            analysis_queue=None,
        )
        
        assert len(results) == 1
        res = results[0]
        assert res["ticker"] == "AAPL"
        assert res["action"] == "HOLD"
        assert res["confidence"] == 0
        assert res["is_timeout_fallback"] is True
        assert res["error_type"] == "timeout"

@pytest.mark.asyncio
async def test_v1_compatible_result_schema():
    """Verify that build_v1_compatible_result constructs a dictionary matching the expected V1 schema."""
    
    # Mocking input schemas
    ticker = "AAPL"
    action = "BUY"
    confidence = 85
    rationale = "Bullish thesis with strong volume and moving average cross."
    cycle_id = "cycle-test-v1"
    total_tokens = 1200
    elapsed_time = 12.54
    stages = ["ontology", "evidence", "sufficiency", "thesis"]
    config_used = "v2_cognition"
    
    mock_sufficiency = MagicMock()
    mock_sufficiency.status = "sufficient"
    
    mock_thesis = MagicMock()
    mock_thesis.action = "BUY"
    mock_thesis.confidence = 85
    mock_thesis.weaknesses = ["Valuation multiple premium"]
    
    mock_debate = MagicMock()
    mock_debate.judge_action = "BUY"
    mock_debate.judge_confidence = 85
    mock_debate.winning_side = "bull"
    mock_debate.integrity_status = "HIGH"
    mock_debate.verified_bull_claims = ["Claim 1", "Claim 2"]
    mock_debate.bull_claims = ["Claim 1", "Claim 2"]
    mock_debate.bear_claims = ["Claim A"]
    mock_debate.verified_bear_claims = []
    mock_debate.unverified_claims = ["Claim A"]
    mock_debate.key_deciding_factor = "Earnings growth rate"
    mock_debate.transcript = "Mock transcript content"
    mock_debate.total_tokens = 800
    mock_debate.original_thesis_status = "HELD"
    mock_debate.original_thesis_explanation = "Held asset analysis"
    
    result = build_v1_compatible_result(
        ticker=ticker,
        action=action,
        confidence=confidence,
        rationale=rationale,
        cycle_id=cycle_id,
        total_tokens=total_tokens,
        elapsed=elapsed_time,
        stages=stages,
        config_used=config_used,
        thesis=mock_thesis,
        sufficiency=mock_sufficiency,
        memory_context={"episode_count": 3, "rule_count": 2},
        debate_result=mock_debate,
        agent_results={"sentiment": "Positive"}
    )
    
    # Assert existence of required V1 keys
    required_keys = [
        "ticker", "action", "confidence", "rationale", "config_used",
        "triage_tier", "escalated", "agent_results", "c_result", "d_result",
        "human_review", "agent_tokens", "rlm_tokens", "total_tokens",
        "total_time_s", "timestamp", "v2_metadata"
    ]
    for key in required_keys:
        assert key in result, f"Result does not contain required key: {key}"
        
    # Check types and properties
    assert result["ticker"] == "AAPL"
    assert result["action"] == "BUY"
    assert result["confidence"] == 85
    assert result["rationale"] == rationale
    assert result["triage_tier"] == "sufficient"
    assert result["escalated"] is True
    assert result["total_tokens"] == total_tokens
    assert result["total_time_s"] == round(elapsed_time, 2)
    
    # Check c_result structure
    assert result["c_result"]["action"] == "BUY"
    assert result["c_result"]["confidence"] == 85
    assert result["c_result"]["rationale"] == rationale
    
    # Check d_result structure
    assert result["d_result"]["action"] == "BUY"
    assert result["d_result"]["confidence"] == 85
    assert result["d_result"]["original_thesis_status"] == "HELD"
    assert result["d_result"]["original_thesis_explanation"] == "Held asset analysis"
    
    # Check v2_metadata structure
    assert result["v2_metadata"]["stages_completed"] == stages
    assert result["v2_metadata"]["sufficiency_status"] == "sufficient"
    assert result["v2_metadata"]["thesis_action"] == "BUY"
    assert result["v2_metadata"]["thesis_confidence"] == 85
    assert result["v2_metadata"]["thesis_weaknesses"] == ["Valuation multiple premium"]
    assert result["v2_metadata"]["memory_episodes"] == 3
    assert result["v2_metadata"]["memory_rules"] == 2
