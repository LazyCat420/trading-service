"""
Integration test for GLiNER shadow mode pipeline.

Verifies:
1. News articles passing through `ensure_facts` trigger shadow entity extraction.
2. Extracted entities land in MongoDB `feature_lineage` collection with `mode="shadow"`.
3. Downstream grounded facts returned to agents remain strictly decoupled from GLiNER in Phase 1.
4. Failures on the feature platform fail open and do not break news ingestion or cycle execution.
"""

import asyncio
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.news_extraction import ensure_facts
from app.services import feature_lineage_store


@pytest.mark.asyncio
async def test_gliner_shadow_pipeline_isolation_and_persistence():
    article_id = f"art_{secrets.token_hex(4)}"
    ticker = "NVDA"
    title = "NVIDIA Reports Record Quarter"
    summary = (
        "NVIDIA Corporation (NVDA) reported record quarterly revenue of $30.0 billion, "
        "up 122% from a year ago. The company raised full-year guidance and CEO Jensen Huang "
        "highlighted surging enterprise demand for Blackwell architecture chips across cloud service "
        "providers and sovereign AI initiatives. Gross margins expanded to 75.1% while data center "
        "revenue reached a record $26.3 billion, up 154% year over year. Financial analysts praised "
        "the stronger-than-expected outlook and raised price targets across the board."
    )
    rows = [(article_id, ticker, title, summary)]

    mock_entities = [
        {"span": "NVDA", "label": "ticker", "start_offset": 20, "end_offset": 24, "score": 0.99},
        {"span": "raised full-year guidance", "label": "event_trigger", "start_offset": 105, "end_offset": 130, "score": 0.95},
    ]

    mock_feature_resp = {
        "request_id": "test-req-gliner",
        "model_id": "gliner",
        "model_version": "gliner-trading-v1",
        "schema_version": "1",
        "input_hash": "mock_hash_123",
        "latency_ms": 40,
        "result": {
            article_id: mock_entities,
        },
    }

    mock_db = MagicMock()
    mock_col = MagicMock()
    mock_db.__getitem__.return_value = mock_col

    # Mock mongo cache empty so it proceeds to extraction
    with patch("app.db.mongo_store.find_docs", return_value=[]), \
         patch("app.db.mongo_store.get_doc_db", return_value=mock_db), \
         patch("app.services.news_extraction.extract_article_facts_with_source") as mock_extract_vllm, \
         patch("app.services.news_extraction._store_facts") as mock_store_facts, \
         patch("app.services.jetson_feature_client.feature_client.extract_entities", new_callable=AsyncMock) as mock_extract_entities:

        mock_extract_vllm.return_value = ([
            {"class": "earnings", "statement": "Quarterly revenue $30.0B", "quote": "revenue of $30.0 billion", "direction": "bullish"}
        ], "vllm")
        mock_extract_entities.return_value = mock_feature_resp

        # Run ensure_facts
        facts_map = await ensure_facts(rows, budget_s=5.0)

        # 1. Downstream facts map must come from the established LLM pipeline, completely unaltered by GLiNER
        assert article_id in facts_map
        assert len(facts_map[article_id]) == 1
        assert facts_map[article_id][0]["statement"] == "Quarterly revenue $30.0B"

        # Allow background shadow task to complete
        await asyncio.sleep(0.1)

        # 2. Assert that feature_client.extract_entities was called
        assert mock_extract_entities.call_count == 1
        call_kwargs = mock_extract_entities.call_args[1]
        assert call_kwargs["documents"][0]["document_id"] == article_id
        assert call_kwargs["documents"][0]["ticker"] == ticker

        # 3. Assert that the shadow feature was saved to MongoDB feature_lineage
        assert mock_col.insert_one.call_count == 1
        stored_doc = mock_col.insert_one.call_args[0][0]
        assert stored_doc["document_id"] == article_id
        assert stored_doc["instrument_id"] == ticker
        assert stored_doc["model_id"] == "gliner"
        assert stored_doc["mode"] == "shadow"
        assert stored_doc["payload"]["entities"] == mock_entities


@pytest.mark.asyncio
async def test_gliner_shadow_pipeline_fail_open_on_service_error():
    """Verifies that if the Jetson Feature Platform is offline or errors, cycle execution does not block."""
    article_id = f"art_fail_{secrets.token_hex(4)}"
    ticker = "AAPL"
    title = "Apple Event Highlights"
    summary = (
        "Apple announced new iPhone hardware with custom silicon and expanded services revenue across global markets. "
        "The company highlighted double-digit revenue growth in its services division, reaching an all-time record, "
        "while active installed devices crossed 2.2 billion units worldwide. Gross margins remained resilient at 46.2%, "
        "beating Wall Street forecasts despite headwinds in greater China, and management announced an expanded share "
        "repurchase authorization of $110 billion alongside a 4% increase in the quarterly cash dividend."
    )
    rows = [(article_id, ticker, title, summary)]

    with patch("app.db.mongo_store.find_docs", return_value=[]), \
         patch("app.services.news_extraction.extract_article_facts_with_source") as mock_extract_vllm, \
         patch("app.services.news_extraction._store_facts"), \
         patch("app.services.jetson_feature_client.feature_client.extract_entities", new_callable=AsyncMock) as mock_extract_entities:

        mock_extract_vllm.return_value = ([], "vllm")
        mock_extract_entities.side_effect = Exception("Jetson 8002 Connection Refused")

        # ensure_facts must succeed without raising
        facts_map = await ensure_facts(rows, budget_s=5.0)
        assert isinstance(facts_map, dict)

        # Allow background shadow task to complete
        await asyncio.sleep(0.1)
        assert mock_extract_entities.call_count == 1
