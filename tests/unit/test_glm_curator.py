"""
Unit tests for GLMCuratorService.
Tests 3-pass multi-sample consensus voting, span extraction, and hallucination rejection.
Follows TDD green/red discipline.
Zero credential leakage: dynamically generated IDs and tokens.
"""

import json
import secrets
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.services.glm_curator_service import GLMCuratorService, ConsensusEntity


@pytest.fixture
def curator():
    return GLMCuratorService(
        base_url="http://10.0.0.16:5591/vllm-shim/gold-spark",
        model_name="GLM-5.3-Flash-EXL3",
        timeout=5.0,
    )


def test_consensus_entity_equality():
    e1 = ConsensusEntity(text="Apple", label="company", start=0, end=5, ticker="AAPL")
    e2 = ConsensusEntity(text="Apple", label="company", start=0, end=5, ticker="AAPL")
    assert e1 == e2
    assert hash(e1) == hash(e2)


def test_filter_consensus_spans(curator):
    # Pass 1 finds Apple (company), AAPL (ticker), and "iPhone" (product - hallucinated once)
    pass1 = [
        ConsensusEntity(text="Apple", label="company", start=0, end=5, ticker="AAPL"),
        ConsensusEntity(text="AAPL", label="ticker", start=7, end=11, ticker="AAPL"),
        ConsensusEntity(text="iPhone", label="financial_metric", start=20, end=26),  # Hallucinated
    ]
    # Pass 2 finds Apple (company), AAPL (ticker)
    pass2 = [
        ConsensusEntity(text="Apple", label="company", start=0, end=5, ticker="AAPL"),
        ConsensusEntity(text="AAPL", label="ticker", start=7, end=11, ticker="AAPL"),
    ]
    # Pass 3 finds Apple (company), AAPL (ticker), and "Asia" (macro_indicator - hallucinated once)
    pass3 = [
        ConsensusEntity(text="Apple", label="company", start=0, end=5, ticker="AAPL"),
        ConsensusEntity(text="AAPL", label="ticker", start=7, end=11, ticker="AAPL"),
        ConsensusEntity(text="Asia", label="macro_indicator", start=30, end=34),  # Hallucinated
    ]

    consensus = curator.filter_consensus_spans([pass1, pass2, pass3], min_agreement=2)
    
    # Only Apple and AAPL appeared in >= 2 passes
    assert len(consensus) == 2
    entity_texts = {e.text for e in consensus}
    assert "Apple" in entity_texts
    assert "AAPL" in entity_texts
    assert "iPhone" not in entity_texts
    assert "Asia" not in entity_texts


@pytest.mark.asyncio
async def test_extract_consensus_entities_mocked(curator):
    text = "NVIDIA (NVDA) reported record Q3 revenue of $18.1B."
    
    # Mock GLM 5.3 responses for 3 passes
    mock_responses = [
        # Pass 1
        {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "entities": [
                            {"text": "NVIDIA", "label": "company", "ticker": "NVDA"},
                            {"text": "NVDA", "label": "ticker", "ticker": "NVDA"},
                            {"text": "revenue", "label": "financial_metric"},
                            {"text": "$18.1B", "label": "financial_metric_value"},
                            {"text": "Q3", "label": "guidance_period"},
                        ]
                    })
                }
            }]
        },
        # Pass 2
        {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "entities": [
                            {"text": "NVIDIA", "label": "company", "ticker": "NVDA"},
                            {"text": "NVDA", "label": "ticker", "ticker": "NVDA"},
                            {"text": "revenue", "label": "financial_metric"},
                            {"text": "$18.1B", "label": "financial_metric_value"},
                            {"text": "Q3", "label": "guidance_period"},
                            {"text": "record", "label": "event_trigger"},  # Single pass only
                        ]
                    })
                }
            }]
        },
        # Pass 3
        {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "entities": [
                            {"text": "NVIDIA", "label": "company", "ticker": "NVDA"},
                            {"text": "NVDA", "label": "ticker", "ticker": "NVDA"},
                            {"text": "revenue", "label": "financial_metric"},
                            {"text": "$18.1B", "label": "financial_metric_value"},
                            {"text": "Q3", "label": "guidance_period"},
                        ]
                    })
                }
            }]
        },
    ]

    call_count = 0
    async def mock_post(*args, **kwargs):
        nonlocal call_count
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json.return_value = mock_responses[call_count]
        call_count += 1
        return resp

    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        results = await curator.annotate_text_with_consensus(text, num_samples=3, min_agreement=2)
        assert len(results) == 5
        texts = [r.text for r in results]
        assert "NVIDIA" in texts
        assert "NVDA" in texts
        assert "revenue" in texts
        assert "$18.1B" in texts
        assert "Q3" in texts
        assert "record" not in texts  # Dropped because only in 1 pass


def test_format_for_gliner_training(curator):
    text = "Apple (AAPL) posted record revenue."
    entities = [
        ConsensusEntity(text="Apple", label="company", start=0, end=5, ticker="AAPL"),
        ConsensusEntity(text="AAPL", label="ticker", start=7, end=11, ticker="AAPL"),
        ConsensusEntity(text="revenue", label="financial_metric", start=26, end=33),
    ]

    formatted = curator.format_for_gliner_training(text, entities)
    assert "tokenized_text" in formatted
    assert "ner" in formatted
    assert formatted["tokenized_text"] == ["Apple", "(AAPL)", "posted", "record", "revenue."]
    assert len(formatted["ner"]) >= 2
