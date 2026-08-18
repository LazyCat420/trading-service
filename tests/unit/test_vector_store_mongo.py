"""Mongo backend of app/db/vector_store.py — store/search against a fake
in-memory collection (no live Mongo needed)."""

import struct

import pytest

from app.db import mongo_store
from app.db.vector_store import VectorStore, _pack_vec, _unpack_matrix


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class _FakeColl:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def create_index(self, *a, **k):
        return None

    def delete_many(self, q):
        keys = [k for k, d in self.docs.items()
                if all(d.get(f) == v for f, v in q.items())]
        for k in keys:
            del self.docs[k]

    def update_one(self, key, update, upsert=False):
        doc = dict(update["$set"])
        self.docs[doc["id"]] = doc

    def bulk_write(self, ops, ordered=True):
        for op in ops:
            doc = dict(op._doc["$set"])
            self.docs[doc["id"]] = doc

    def count_documents(self, q, limit=0):
        n = sum(1 for d in self.docs.values()
                if all(d.get(f) == v for f, v in q.items()))
        return min(n, limit) if limit else n

    def _match(self, d, q):
        for f, v in q.items():
            if f == "$or":
                if not any(self._match(d, sub) for sub in v):
                    return False
            elif isinstance(v, dict) and "$in" in v:
                if d.get(f) not in v["$in"]:
                    return False
            elif d.get(f) != v:
                return False
        return True

    def find(self, q, projection=None):
        return _FakeCursor([dict(d) for d in self.docs.values() if self._match(d, q)])


@pytest.fixture
def mongo_vs(monkeypatch):
    fake = _FakeColl()
    monkeypatch.setattr(mongo_store, "_BACKENDS", {"embeddings": "mongo"})
    monkeypatch.setattr(VectorStore, "_mongo_coll", classmethod(lambda cls: fake))
    vs = VectorStore()
    return vs, fake


def _store(vs, sid, ticker, vec, table="news_articles"):
    return vs.store_embedding(table, sid, ticker, f"preview {sid}", vec)


def test_pack_roundtrip():
    vec = [0.1, -0.5, 3.25]
    m, kept = _unpack_matrix([{"embedding": _pack_vec(vec), "id": "x"}])
    assert m.shape == (1, 3)
    assert abs(m[0][1] - (-0.5)) < 1e-6


def test_store_and_search_ranks_by_cosine(mongo_vs):
    vs, fake = mongo_vs
    _store(vs, "a", "NVDA", [1.0, 0.0, 0.0])
    _store(vs, "b", "NVDA", [0.9, 0.1, 0.0])
    _store(vs, "c", None, [0.0, 1.0, 0.0])
    assert len(fake.docs) == 3

    hits = vs.search_cosine([1.0, 0.0, 0.0], ticker="NVDA", top_k=2)
    assert [h["source_id"] for h in hits] == ["a", "b"]
    assert hits[0]["score"] > 0.999


def test_ticker_filter_includes_null_macro_rows(mongo_vs):
    vs, _ = mongo_vs
    _store(vs, "macro", None, [1.0, 0.0, 0.0])
    _store(vs, "other", "AAPL", [1.0, 0.0, 0.0])
    hits = vs.search_cosine([1.0, 0.0, 0.0], ticker="NVDA", top_k=10)
    assert [h["source_id"] for h in hits] == ["macro"]


def test_source_filter(mongo_vs):
    vs, _ = mongo_vs
    _store(vs, "n1", "NVDA", [1.0, 0.0, 0.0], table="news_articles")
    _store(vs, "m1", "NVDA", [1.0, 0.0, 0.0], table="canonical_memories")
    hits = vs.search_cosine([1.0, 0.0, 0.0], source_filter="canonical_memories", top_k=5)
    assert [h["source_id"] for h in hits] == ["m1"]


def test_store_replaces_prior_row_for_same_source(mongo_vs):
    vs, fake = mongo_vs
    _store(vs, "a", "NVDA", [1.0, 0.0, 0.0])
    vs.store_embedding("news_articles", "a", "NVDA", "updated", [0.0, 1.0, 0.0],
                       embedding_id="new-id")
    assert len(fake.docs) == 1
    assert list(fake.docs.values())[0]["content_preview"] == "updated"


def test_zero_vector_rejected(mongo_vs):
    vs, fake = mongo_vs
    assert _store(vs, "z", "NVDA", [0.0, 0.0, 0.0]) == ""
    assert len(fake.docs) == 0


def test_dim_mismatch_returns_empty(mongo_vs):
    vs, _ = mongo_vs
    _store(vs, "a", "NVDA", [1.0, 0.0, 0.0])
    assert vs.search_cosine([1.0, 0.0], ticker="NVDA") == []


def test_exists_and_existing_source_ids(mongo_vs):
    vs, _ = mongo_vs
    _store(vs, "a", "NVDA", [1.0, 0.0, 0.0])
    assert vs.exists("news_articles", "a")
    assert not vs.exists("news_articles", "b")
    assert vs.existing_source_ids("news_articles", ["a", "b"]) == {"a"}


def test_store_batch(mongo_vs):
    vs, fake = mongo_vs
    n = vs.store_batch([
        {"source_table": "news_articles", "source_id": "x", "ticker": "T",
         "content_preview": "p", "embedding": [1.0, 0.0, 0.0]},
        {"source_table": "news_articles", "source_id": "y", "ticker": "T",
         "content_preview": "p", "embedding": [0.0, 0.0, 0.0]},  # zero → skipped
    ])
    assert n == 1
    assert len(fake.docs) == 1


def test_pg_default_backend_untouched(monkeypatch):
    monkeypatch.setattr(mongo_store, "_BACKENDS", {})
    vs = VectorStore()
    assert vs._writes_pg() and not vs._writes_mongo() and not vs._reads_mongo()
