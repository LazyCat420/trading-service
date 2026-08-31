"""The memory-injection seam must not raise on a ghost import.

2026-08-31 finding: commit b6b29d3 (08-18) deleted _ensure_schema from
app/db/memory_repo while app/services/memory/repository.py kept two
function-local imports of it. Every MemoryRetriever.retrieve therefore raised
ImportError, orchestrator.py swallowed it as "Memory retrieval failed
(non-fatal)", and NO memory tier reached any agent prompt for ~13 days. The
only test of the path mocked fetch_candidate_memories — the exact seam that
hid the break. These tests import the REAL modules.
"""
from app.services.memory.repository import MemoryRepository


def test_fetch_candidate_memories_does_not_import_a_ghost(monkeypatch):
    """RED on the broken code: ImportError from the function-local import."""
    import app.db.mongo_query as mongo_query
    monkeypatch.setattr(mongo_query, "find_dicts", lambda *a, **k: [])
    # Must not raise. On the pre-fix code this raises ImportError before the
    # query ever runs.
    assert MemoryRepository.fetch_candidate_memories("AAPL", sector="tech") == []


def test_get_memories_by_ticker_does_not_import_a_ghost(monkeypatch):
    import app.db.mongo_query as mongo_query
    monkeypatch.setattr(mongo_query, "find_rows", lambda *a, **k: [])
    assert MemoryRepository.get_memories_by_ticker("AAPL") == []


def test_memory_repo_still_exports_ensure_schema():
    """scripts/init_test_db.py:187 also references this symbol by name."""
    import app.db.memory_repo as memory_repo
    assert callable(getattr(memory_repo, "_ensure_schema", None))


def test_memory_context_flag_is_registered():
    from app.services.parameter_store import PARAMETER_REGISTRY
    spec = PARAMETER_REGISTRY.get("MEMORY_CONTEXT_ENABLED")
    assert spec is not None, "MEMORY_CONTEXT_ENABLED missing from the registry"
    assert (spec.min_value, spec.max_value) == (0, 1)
