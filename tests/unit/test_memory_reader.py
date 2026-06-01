from unittest.mock import MagicMock, patch
import pytest
from app.cognition.memory.reader import read_memories
from app.cognition.memory.models import MemoryType, MemoryStatus

@patch("app.cognition.memory.reader.get_db")
@patch("app.cognition.memory.reader._ensure_schema")
def test_read_memories_filters_episodic_by_completed_cycle(mock_ensure, mock_get_db):
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    executed_queries = []
    
    def side_effect_execute(query, params=None):
        executed_queries.append((query, params))
        mock_cursor = MagicMock()
        mock_cursor.description = [("id",), ("memory_type",), ("entity_id",), ("payload_json",)]
        mock_cursor.fetchall.return_value = [
            ("mem-1", "episodic", "AAPL", '{"event_type": "pipeline_run", "action": "BUY"}')
        ]
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute
    
    results = read_memories("AAPL", memory_types=["episodic"])
    
    # Verify that the query contains the EXISTS clause with status = 'done'
    last_query = executed_queries[0][0]
    assert "cb.status = 'done'" in last_query
    assert "e.memory_type != 'episodic'" in last_query
    assert "cognition_episodic_memories" in last_query
    assert "cycle_benchmarks" in last_query
    assert results[0].id == "mem-1"
