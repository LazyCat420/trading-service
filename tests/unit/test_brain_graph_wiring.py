"""Brain-graph read/write loop wiring — 2026-07-18 audit fixes.

Covers:
  - graph_sync garbage guard (parse failures must not become Claim nodes)
  - ENABLE_ONTOLOGY_GRAPH actually gating the sync
  - build_brain_graph_block feeding build_memory_addenda (the live prompt path)
  - BrainGraph.activate_and_persist writing the activation column
  - eval_worker ACTIVATE_BRAIN_GRAPH handler completing with progress updates
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.cognition.ontology.graph_sync import _clean_text, sync_desk_to_graph


def _mock_db_ctx():
    """A MagicMock whose get_db() context manager yields a db mock."""
    db = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    get_db = MagicMock(return_value=ctx)
    return get_db, db


def _desk(**overrides):
    desk = MagicMock()
    desk.ticker = "TEST"
    desk.regime_classification = {"regime": "bull"}
    desk.fundamental_report = {"summary": "Strong cash flows and cheap valuation", "thesis_direction": "LONG", "confidence": 70}
    desk.quant_report = None
    desk.tournament_result = {}
    desk.trade_decision = {"action": "BUY", "confidence": 75, "reasoning": "Owner-earnings yield attractive"}
    desk.final_decision = None
    for k, v in overrides.items():
        setattr(desk, k, v)
    return desk


class TestGarbageGuard:
    def test_clean_text_passes_real_text(self):
        assert _clean_text("Strong fundamentals with cash flow support") != ""

    @pytest.mark.parametrize("garbage", [
        "Failed to parse thesis\n\nData timeframe: ...",
        "  PARSE ERROR in upstream model",
        "No response from LLM",
        "",
        None,
        "short",
    ])
    def test_clean_text_rejects_garbage(self, garbage):
        assert _clean_text(garbage) == ""

    def test_sync_never_writes_parse_failures(self):
        desk = _desk(
            fundamental_report={"summary": "Failed to parse thesis", "confidence": 75},
            trade_decision={"action": "HOLD", "confidence": 75, "reasoning": "Failed to parse thesis\n\nMemory context: ..."},
        )
        with patch("app.cognition.ontology.graph_sync.BrainGraph") as bg, \
             patch("app.cognition.ontology.graph_sync.mongo_store"):
            sync_desk_to_graph(desk, "cycle-1")
            texts = [str(c) for c in bg.upsert_node.call_args_list]
            assert texts, "sync should still record the clean claims"
            assert not any("Failed to parse" in t for t in texts)
            # decision claim survives, minus the garbage reasoning
            assert any("decision HOLD" in t for t in texts)


class TestFlagGate:
    def test_flag_off_skips_sync_entirely(self):
        from app.config.config_cognition import cognition_settings
        with patch.object(cognition_settings, "ENABLE_ONTOLOGY_GRAPH", False), \
             patch("app.cognition.ontology.graph_sync.BrainGraph") as bg, \
             patch("app.cognition.ontology.graph_sync.mongo_store"):
            sync_desk_to_graph(_desk(), "cycle-1")
            bg.upsert_node.assert_not_called()

    def test_flag_off_skips_prompt_block(self):
        from app.config.config_cognition import cognition_settings
        from app.services.retrieval_context import build_brain_graph_block
        with patch.object(cognition_settings, "ENABLE_ONTOLOGY_GRAPH", False):
            assert build_brain_graph_block("TEST") == ""


class TestPromptWiring:
    def test_brain_graph_block_caps_and_returns_context(self):
        from app.services import retrieval_context as rc
        with patch("app.cognition.ontology.ontology_builder.BrainGraph.get_activated_context",
                   return_value="## Brain Graph Context for TEST\n- claim (activation=80%)"):
            block = rc.build_brain_graph_block("TEST")
        assert "Brain Graph Context" in block
        assert len(block) <= rc.BLOCK_MAX_CHARS + 20  # _cap adds an elision marker

    def test_memory_addenda_includes_graph_block(self):
        from app.services import retrieval_context as rc
        with patch.object(rc, "build_working_memory_block", return_value=""), \
             patch.object(rc, "build_retrieved_context", return_value=""), \
             patch.object(rc, "build_brain_graph_block", return_value="## Brain Graph Context for TEST"):
            assert "Brain Graph Context" in rc.build_memory_addenda("TEST")

    def test_addenda_empty_when_all_blocks_empty(self):
        from app.services import retrieval_context as rc
        with patch.object(rc, "build_working_memory_block", return_value=""), \
             patch.object(rc, "build_retrieved_context", return_value=""), \
             patch.object(rc, "build_brain_graph_block", return_value=""):
            assert rc.build_memory_addenda("TEST") == ""


class TestActivatePersist:
    def test_activation_column_written(self):
        from app.cognition.ontology.ontology_builder import BrainGraph
        subgraph = {
            "nodes": [{"id": "TEST", "activation": 1.0}, {"id": "claim_abc", "activation": 0.42}],
            "edges": [],
            "stats": {"total_activated": 2, "hops_used": 3, "seed_nodes": ["TEST"]},
        }
        updates = []
        with patch.object(BrainGraph, "spreading_activation", return_value=subgraph), \
             patch("app.cognition.ontology.ontology_builder.mongo_store.update_docs",
                   side_effect=lambda coll, q, u, **kw: updates.append((coll, q, u)) or 1):
            stats = BrainGraph.activate_and_persist("TEST")
        assert stats["persisted"] == 2
        assert all(coll == "ontology_nodes" for coll, _, _ in updates)
        # per-ticker decay: a multiplicative 0.8 over the already-active nodes
        assert any(u.get("$mul", {}).get("activation") == 0.8 for _, _, u in updates)
        # and the freshly activated values written per node
        written = {q.get("id"): u["$set"]["activation"]
                   for _, q, u in updates if "$set" in u and "activation" in u["$set"]
                   and "id" in q}
        assert written == {"TEST": 1.0, "claim_abc": 0.42}

    def test_no_seeds_is_a_noop(self):
        from app.cognition.ontology.ontology_builder import BrainGraph
        with patch("app.cognition.ontology.ontology_builder.mongo_query.find_rows",
                   return_value=[]), \
             patch("app.cognition.ontology.ontology_builder.mongo_store.update_docs") as upd:
            stats = BrainGraph.activate_and_persist(None)
        assert stats["persisted"] == 0
        upd.assert_not_called()  # no seeds ⇒ nothing written at all


class TestSparseGNN:
    def test_activation_spreads_to_neighbors_only(self):
        from app.cognition.ontology.gnn_engine import GNNEngine
        gnn = GNNEngine(["a", "b", "isolated"], [("a", "b", 0.8)])
        acts = gnn.message_passing({"a": 1.0}, layers=3)
        assert acts["a"] == 1.0  # seed re-injected each layer
        assert acts["b"] > 0.0
        assert acts["isolated"] == 0.0

    def test_unknown_seed_and_edge_nodes_ignored(self):
        from app.cognition.ontology.gnn_engine import GNNEngine
        gnn = GNNEngine(["a"], [("a", "ghost", 0.5)])
        acts = gnn.message_passing({"a": 1.0, "phantom": 1.0}, layers=2)
        assert set(acts) == {"a"}

    def test_large_graph_is_fast_and_lean(self):
        # 12k nodes / 15k edges — the prod scale that made the dense
        # implementation burn minutes of CPU. Sparse must run in well under a
        # second; this guards against a dense N×N matrix creeping back in.
        import time
        from app.cognition.ontology.gnn_engine import GNNEngine
        n = 12_000
        nodes = [f"n{i}" for i in range(n)]
        edges = [(f"n{i}", f"n{(i * 7 + 1) % n}", 0.5) for i in range(15_000)]
        start = time.monotonic()
        gnn = GNNEngine(nodes, edges)
        acts = gnn.message_passing({"n0": 1.0}, layers=3)
        assert time.monotonic() - start < 5.0
        assert acts["n0"] == 1.0


class TestActivateCommandHandler:
    def test_handler_completes_with_progress(self):
        from app.autoresearch import eval_worker
        updates = []
        with patch("app.cognition.ontology.ontology_builder.BrainGraph.seed_from_ticker_metadata",
                   return_value=7) as seed, \
             patch("app.cognition.ontology.ontology_builder.BrainGraph.activate_and_persist",
                   return_value={"total_activated": 5, "persisted": 5, "seed_nodes": ["TEST"], "hops_used": 3}), \
             patch.object(eval_worker.mongo_store, "update_docs",
                          side_effect=lambda coll, q, u, **kw: updates.append((coll, q, u)) or 1):
            asyncio.run(eval_worker.run_activate_brain_graph("job-1", {"ticker": "test", "max_hops": 3}))
        seed.assert_called_once_with("TEST")  # payload ticker is upcased
        assert all(coll == "system_commands" for coll, _, _ in updates)
        final = updates[-1][2]["$set"]
        assert final["status"] == "completed"
        assert final["progress"] == 100

    def test_handler_without_ticker_skips_seeding(self):
        from app.autoresearch import eval_worker
        with patch("app.cognition.ontology.ontology_builder.BrainGraph.seed_from_ticker_metadata") as seed, \
             patch("app.cognition.ontology.ontology_builder.BrainGraph.activate_and_persist",
                   return_value={"total_activated": 0, "persisted": 0, "seed_nodes": []}), \
             patch.object(eval_worker.mongo_store, "update_docs", return_value=1):
            asyncio.run(eval_worker.run_activate_brain_graph("job-2", {"ticker": None}))
        seed.assert_not_called()
