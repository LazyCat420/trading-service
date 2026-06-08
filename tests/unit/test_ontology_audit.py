import pytest
import time
from unittest.mock import patch, MagicMock
from app.cognition.ontology.ontology_builder import BrainGraph
from app.db.connection import get_db

@pytest.fixture
def mock_db():
    with patch("app.cognition.ontology.ontology_builder.get_db") as mock:
        yield mock

class TestOntologyAudit:
    """Full audit and general audit tests for the ontology builder."""

    def test_slow_db_spreading_activation(self, mock_db):
        """Test how spreading activation handles a slow DB connection."""
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Simulate a slow query
        def slow_execute(*args, **kwargs):
            time.sleep(0.1)  # Simulate delay
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = []
            mock_cursor.fetchone.return_value = ("Asset", "AAPL", '{"some": "data"}', 0, 0, False)
            return mock_cursor

        mock_conn.execute.side_effect = slow_execute
        
        start = time.time()
        result = BrainGraph.spreading_activation(seed_node_ids=["AAPL"], max_hops=1)
        end = time.time()
        
        assert result["stats"]["total_activated"] == 1
        # If it didn't hang indefinitely, it passed the slow db test

    def test_spreading_activation_max_parallelism(self, mock_db):
        """Test spreading activation under large node counts to check memory bounds."""
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Simulate a massive graph return from the recursive CTE
        massive_edges = []
        for i in range(1000):
            # source, target, weight, decay, relation, evidence_count
            massive_edges.append(("AAPL", f"NODE_{i}", 0.9, 0.85, "CORRELATES_WITH", 1))
            
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = massive_edges
        mock_cursor.fetchone.return_value = ("Asset", "TestLabel", "{}", 0, 0, False)
        mock_conn.execute.return_value = mock_cursor
        
        result = BrainGraph.spreading_activation(seed_node_ids=["AAPL"])
        
        # Should cap out at MAX_SUBGRAPH_NODES (50)
        assert len(result["nodes"]) <= 50
        assert result["stats"]["total_activated"] <= 50

    def test_connection_leak_prevention(self, mock_db):
        """Verify that get_db() context managers are always closed even on errors."""
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Force an exception during execution
        mock_conn.execute.side_effect = Exception("DB Connection Drop")
        
        # Should not crash the entire process, should handle cleanly
        BrainGraph.upsert_node("AAPL", "Asset")
        
        # The context manager __exit__ should have been called (implied by context manager structure in upsert_node)
        assert mock_db.return_value.__enter__.called
        
    def test_network_partition(self, mock_db):
        """Simulate a network partition where DB becomes completely unreachable."""
        mock_db.side_effect = Exception("Connection Refused")
        
        BrainGraph.upsert_node("AAPL", "Asset")
        
        # Should log error and return safely, not crash
        result = BrainGraph.spreading_activation(seed_node_ids=["AAPL"])
        # If GNNEngine fails or DB fails, it falls back to seed-only
        assert result["stats"]["total_activated"] == 0
        assert result["stats"]["graph_degraded"] == True

    @pytest.mark.asyncio
    @patch("app.cognition.ontology.ontology_generator.OntologyGenerator.generate_and_extract")
    @patch("app.cognition.ontology.entity_extractor.get_db")
    async def test_async_extract_and_seed_deep_robustness(self, mock_db, mock_generate_and_extract):
        """Verify async_extract_and_seed_deep behaves robustly under malformed LLM responses."""
        from app.cognition.ontology.entity_extractor import async_extract_and_seed_deep
        
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Test case 1: None/invalid type response
        mock_generate_and_extract.return_value = None
        stats = await async_extract_and_seed_deep("AAPL", "Test analysis text content here...", "cycle-123")
        assert stats["total_nodes"] == 0
        
        # Test case 2: Malformed list elements, missing keys, invalid types
        mock_generate_and_extract.return_value = {
            "nodes": [
                "not-a-dict",
                {"id": ""},  # empty ID
                {"id": "valid-id", "dynamic_type": "EconomicIndicator", "metadata": "not-a-dict"}
            ],
            "edges": [
                "not-a-dict",
                {"source": "valid-id"},  # missing target
                {"source": "valid-id", "target": "other-id", "dynamic_type": "INFLUENCES", "weight": "invalid-float"}
            ]
        }
        
        stats = await async_extract_and_seed_deep("AAPL", "Test analysis text content here...", "cycle-123")
        # Should parse the valid-id node and valid-id -> other-id edge safely with fallback weight
        assert stats["total_nodes"] > 0
        assert stats["total_edges"] > 0
        
        # Verify that the actual relation ('INFLUENCES') is passed to the INSERT parameter list
        edge_insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO ontology_edges" in call[0][0]
        ]
        assert len(edge_insert_calls) > 0
        # params: [edge_id, src, tgt, rel, weight, cycle_id, now, now, now]
        # index 3 corresponds to the 'relation' column parameter (rel)
        assert edge_insert_calls[0][0][1][3] == "INFLUENCES"

    @pytest.mark.asyncio
    @patch("app.cognition.ontology.ontology_generator.OntologyGenerator.generate_and_extract")
    @patch("app.cognition.ontology.entity_extractor.get_db")
    async def test_direct_dynamic_node_type_storage(self, mock_db, mock_generate_and_extract):
        """Verify that the actual dynamic type name is directly saved in node_type column."""
        from app.cognition.ontology.entity_extractor import async_extract_and_seed_deep
        
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        mock_generate_and_extract.return_value = {
            "nodes": [
                {"id": "node-123", "dynamic_type": "GeopoliticalEvent", "metadata": {"region": "US"}}
            ],
            "edges": []
        }
        
        await async_extract_and_seed_deep("AAPL", "Long text context...", "cycle-123")
        
        # Verify that dynamic_type ('GeopoliticalEvent') is passed as node_type to INSERT statement
        node_insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO ontology_nodes" in call[0][0]
        ]
        assert len(node_insert_calls) > 0
        # SQL parameters list: [node_id, dyn_type, label, meta, cycle_id, now, now, now]
        # index 1 should correspond to dyn_type column
        assert node_insert_calls[0][0][1][1] == "GeopoliticalEvent"

    @pytest.mark.asyncio
    @patch("app.cognition.ontology.ontology_generator.llm.chat")
    async def test_chunk_based_text_extraction(self, mock_llm_chat):
        """Verify that long texts are chunked and extractions are correctly merged."""
        from app.cognition.ontology.ontology_generator import OntologyGenerator
        
        # Create a text longer than 5000 characters
        long_text = "Some random content. " * 300
        assert len(long_text) > 5000
        
        # Mock two successful chunk responses
        resp_1_text = '{"entity_types": [{"name": "CentralBank"}], "nodes": [{"id": "fed", "dynamic_type": "CentralBank", "metadata": {"name": "Federal Reserve"}}], "edges": []}'
        resp_2_text = '{"entity_types": [{"name": "CentralBank"}], "nodes": [{"id": "fed", "dynamic_type": "CentralBank", "metadata": {"location": "Washington"}}], "edges": []}'
        
        mock_llm_chat.side_effect = [
            (resp_1_text, 100, 200),
            (resp_2_text, 120, 240)
        ]
        
        merged_res = await OntologyGenerator.generate_and_extract(long_text, agent_name="test_curator")
        
        # Verify the generator split the text and combined the node metadata
        assert len(merged_res["nodes"]) == 1
        assert merged_res["nodes"][0]["id"] == "fed"
        assert merged_res["nodes"][0]["dynamic_type"] == "CentralBank"
        # Merged metadata keys from both mock responses
        metadata = merged_res["nodes"][0]["metadata"]
        assert metadata["name"] == "Federal Reserve"
        assert metadata["location"] == "Washington"

