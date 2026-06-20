import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from app.processors.ticker_extractor import extract_and_validate, TickerMatch
from app.services.vllm_client import llm

@pytest.fixture
def mock_prism_endpoint():
    with patch("app.services.prism_client.PrismClient._call_endpoint", new_callable=AsyncMock) as mock_call:
        yield mock_call

@pytest.mark.asyncio
async def test_ticker_extractor_json_fallback(mock_prism_endpoint):
    """
    Audit test: verifies that ticker extraction handles malformed LLM JSON.
    Simulates the exact error from the user report where the bracket failed to finish.
    """
    # 1. Provide a malformed JSON response (unclosed bracket/array)
    malformed_response = '[{"symbol": "ALICE", "is_stock": true' 

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "response": {
            "choices": [
                {
                    "message": {
                        "content": malformed_response
                    }
                }
            ]
        }
    }
    mock_prism_endpoint.return_value = mock_resp
    
    # 2. Run through the pipeline function
    # Provide explicit financial syntax ("bought ALICE") to boost confidence > 0.40
    # so it gets sent to the LLM validator.
    text = "I bought ALICE today for my portfolio."
    
    # Clear cache
    extract_and_validate._llm_cache = {}

    matches = await extract_and_validate(text)
    
    # Verify that we gracefully parsed the malformed JSON without crashing,
    # defaulting to is_stock=True (the fallback behavior).
    # 'ALICE' is recognized as a bare cap and validated.
    assert any(m.symbol == "ALICE" for m in matches)
    assert extract_and_validate._llm_cache["ALICE::I bought ALICE today for my portfolio."] is True


@pytest.mark.asyncio
async def test_ticker_extractor_json_valid(mock_prism_endpoint):
    """
    Audit test: verifies that ticker extraction handles valid LLM JSON.
    """
    # 1. Provide a valid JSON response as an array
    valid_response = '[{"symbol": "BOB", "is_stock": false, "reason": "Not a stock"}]' 

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "response": {
            "choices": [
                {
                    "message": {
                        "content": valid_response
                    }
                }
            ]
        }
    }
    mock_prism_endpoint.return_value = mock_resp
    
    extract_and_validate._llm_cache = {}
    
    text = "I bought BOB today."
    matches = await extract_and_validate(text)
    
    # Should be rejected
    assert not any(m.symbol == "BOB" for m in matches)
    assert extract_and_validate._llm_cache["BOB::I bought BOB today."] is False

@pytest.mark.asyncio
async def test_decision_engine_json_fallback():
    """
    Audit test: verify that the RLM fallback parser gracefully handles malformed 
    LLM output that fails to close JSON brackets.
    """
    from app.utils.text_utils import parse_trading_decision
    
    # 1. Provide an unclosed FINAL bracket with action and confidence
    malformed_response = 'FINAL({\n"action": "BUY",\n"confidence": 85'
    
    # 2. Parse the decision
    decision = parse_trading_decision(malformed_response)
    
    # 3. Verify fallback parser extracted action and confidence from the broken JSON
    assert decision.get("action") == "BUY"
    assert decision.get("confidence") == 85


@pytest.mark.asyncio
async def test_glance_tier_parsing(mock_prism_endpoint):
    """
    Audit test: verify ticker triage handles glance checks properly with Prism standard JSON.
    """
    from app.pipeline.analysis.decision_engine import analyze_ticker
    
    # Mock data completeness to avoid blocking
    with patch("app.pipeline.data.data_completeness.check_and_fill", return_value={"filled": []}):
        with patch("app.pipeline.data.data_completeness.check_data_sufficiency", return_value={"sufficient": True}):
            with patch("app.utils.payload_gate.gate_check"):
                with patch("app.utils.payload_gate.log_transition"):
                    with patch("app.data.market_data_store.get_latest_snapshot", return_value=None):
                        with patch("app.tools.portfolio_tools.get_position_context", return_value={"held": False}):
                            with patch("app.pipeline.analysis.thesis_store.get_thesis") as mock_thesis:
                                
                                mock_thesis.return_value = MagicMock(verdict="BUY", confidence=80)

                                mock_resp = MagicMock()
                                mock_resp.json.return_value = {
                                    "response": {
                                        "choices": [
                                            {
                                                "message": {
                                                    "content": "SKIP No material change detected."
                                                }
                                            }
                                        ]
                                    }
                                }
                                mock_prism_endpoint.return_value = mock_resp
                                
                                # Run analyze_ticker with glance tier
                                result = await analyze_ticker("TEST", triage_tier="glance")
                                
                                # Verify it skipped
                                assert result.get("glance_skipped") is True
                                assert result.get("action") == "BUY"
                                assert result.get("triage_tier") == "glance"
