import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from app.services.memory.retriever import MemoryRetriever

@pytest.fixture
def mock_raw_memories():
    return [
        {
            "id": "mem_id_1",
            "type": "conviction",
            "ticker": "AAPL",
            "sector": "Technology",
            "summary": "Apple has strong iPhone demand",
            "tags": "[\"iphone\", \"demand\"]",
            "confidence_score": 0.8,
            "status": "active",
            "updated_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        },
        {
            "id": "mem_id_2",
            "type": "conviction",
            "ticker": "AAPL",
            "sector": "Technology",
            "summary": "Apple services revenue is growing",
            "tags": "[\"services\"]",
            "confidence_score": 0.9,
            "status": "active",
            "updated_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        }
    ]

@patch("app.services.memory.retriever.fetch_candidate_memories")
@patch("app.services.embedding_service.embedder.embed_text")
@patch("app.db.vector_store.vector_store.search_cosine")
def test_memory_retriever_vector_boost(mock_search_cosine, mock_embed_text, mock_fetch_candidate_memories, mock_raw_memories):
    # Mock SQL candidate fetching
    mock_fetch_candidate_memories.return_value = mock_raw_memories

    # Mock embedder
    mock_embed_text.return_value = [0.1] * 384

    # Mock vector store search. mem_id_2 gets a vector similarity match, mem_id_1 does not.
    mock_search_cosine.return_value = [
        {
            "id": "vec_id_2",
            "source_table": "canonical_memories",
            "source_id": "mem_id_2",
            "ticker": "AAPL",
            "content_preview": "Apple services revenue is growing",
            "score": 0.85
        }
    ]

    # Run the retriever
    results = MemoryRetriever.retrieve("AAPL", sector="Technology", tags=["services"])

    # Verify that we retrieved both memories
    assert len(results) == 2

    # Find the memories in the results
    m1_res = next(r for r in results if r["memory_id"] == "mem_id_1")
    m2_res = next(r for r in results if r["memory_id"] == "mem_id_2")

    # mem_id_2 should have a higher score because of the vector search boost (similarity 0.85 * 10 = +8.5 boost)
    assert m2_res["score"] > m1_res["score"] + 5.0
