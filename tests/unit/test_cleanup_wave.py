"""Cleanup-wave regression guards: zero-vector rejection, embedding dedup,
and sell-side trigger retirement under hard_stop exit ownership."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.db.vector_store import VectorStore
from app.trading import order_triggers as ot


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows_for_update=None):
        self.executed = []
        self._rows_for_update = rows_for_update or []

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))
        if sql.strip().upper().startswith("UPDATE"):
            return _FakeResult(self._rows_for_update)
        return _FakeResult()


def _fake_get_db(db):
    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *a):
            return False

    return lambda: _Ctx()


def test_zero_vector_rejected_before_db(monkeypatch):
    def _boom():
        raise AssertionError("DB must not be touched for a zero vector")

    monkeypatch.setattr("app.db.vector_store.get_db", _boom)
    vs = VectorStore()
    assert vs.store_embedding("t", "id1", "AAPL", "x", [0.0] * 384) == ""
    assert vs.store_embedding("t", "id1", "AAPL", "x", []) == ""


def test_store_embedding_deletes_prior_rows_for_source(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr("app.db.vector_store.get_db", _fake_get_db(db))
    vs = VectorStore()
    eid = vs.store_embedding("canonical_memories", "mem-1", "AAPL", "x", [0.1, 0.2])
    assert eid
    assert any(s.startswith("DELETE FROM embeddings WHERE source_table") for s in db.executed)
    delete_idx = next(i for i, s in enumerate(db.executed) if s.startswith("DELETE"))
    insert_idx = next(i for i, s in enumerate(db.executed) if s.startswith("INSERT"))
    assert delete_idx < insert_idx


def test_deactivate_sell_side_triggers_counts_retired(monkeypatch):
    db = _FakeDB(rows_for_update=[("id1",), ("id2",)])
    monkeypatch.setattr(ot, "get_db", _fake_get_db(db))
    assert ot.deactivate_sell_side_triggers("bot-x", "DIS") == 2
    assert any("trigger_type IN ('stop_loss', 'take_profit')" in s for s in db.executed)


def test_deactivate_sell_side_triggers_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ot, "get_db", _boom)
    assert ot.deactivate_sell_side_triggers("bot-x", "DIS") == 0
