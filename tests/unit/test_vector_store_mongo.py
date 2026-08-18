"""Mongo backend of app/db/vector_store.py — store/search against a fake
in-memory collection (no live Mongo needed)."""

import struct
from pathlib import Path

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


class _FakeResult:
    """pymongo returns result objects, not None — mongo_store reads
    .deleted_count / .modified_count off them."""

    def __init__(self, deleted=0, modified=0, upserted=None, inserted=None):
        self.deleted_count = deleted
        self.modified_count = modified
        self.upserted_id = upserted
        self.inserted_ids = inserted or []


class _FakeColl:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def create_index(self, *a, **k):
        return None

    def delete_many(self, q, session=None, **kw):
        keys = [k for k, d in self.docs.items()
                if all(d.get(f) == v for f, v in q.items())]
        for k in keys:
            del self.docs[k]
        return _FakeResult(deleted=len(keys))

    def update_one(self, key, update, upsert=False, session=None, **kw):
        doc = dict(update["$set"])
        self.docs[doc["id"]] = doc
        return _FakeResult(modified=1, upserted=doc["id"] if upsert else None)

    def update_many(self, q, update, upsert=False, session=None, **kw):
        """`mongo_store.update_docs` calls update_many, not update_one.

        The fake only had update_one, so every write that went through
        mongo_store rather than through the raw collection missed the fake
        entirely and hit the real driver.
        """
        matched = [k for k, d in self.docs.items() if self._match(d, q)]
        setter = update.get("$set", {})
        for k in matched:
            self.docs[k].update(setter)
        if not matched and upsert:
            doc = dict(q)
            doc.update(setter)
            key = doc.get("id") or f"upserted-{len(self.docs)}"
            doc.setdefault("id", key)
            self.docs[key] = doc
            return _FakeResult(modified=0, upserted=key)
        return _FakeResult(modified=len(matched))

    def bulk_write(self, ops, ordered=True, session=None, **kw):
        for op in ops:
            doc = dict(op._doc["$set"])
            self.docs[doc["id"]] = doc
        return _FakeResult(modified=len(ops))

    def insert_many(self, docs, ordered=True, session=None, **kw):
        ids = []
        for d in docs:
            doc = dict(d)
            key = doc.get("id") or f"ins-{len(self.docs)}"
            doc.setdefault("id", key)
            self.docs[key] = doc
            ids.append(key)
        return _FakeResult(inserted=ids)

    def count_documents(self, q, limit=0, session=None, **kw):
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

    def find(self, q, projection=None, session=None, **kw):
        return _FakeCursor([dict(d) for d in self.docs.values() if self._match(d, q)])


class _FakeDb:
    """Just enough of a pymongo Database to hand back the one fake collection.

    `vector_store` reaches Mongo two ways — `_mongo_coll()` for the search
    paths and `mongo_store.delete_docs`/`update_docs`/`find_docs` for the write
    paths. Patching only `_mongo_coll` covered the first and left the second
    opening a REAL connection: these tests were writing embeddings to the
    production collection, and the store guard is what surfaced it. Patching
    `get_doc_db` covers both, because every mongo_store helper resolves its
    collection through it.
    """

    def __init__(self, coll):
        self._coll = coll

    def __getitem__(self, _name):
        return self._coll


@pytest.fixture
def mongo_vs(monkeypatch):
    fake = _FakeColl()
    monkeypatch.setattr(mongo_store, "_BACKENDS", {"embeddings": "mongo"})
    monkeypatch.setattr(mongo_store, "get_doc_db", lambda: _FakeDb(fake))
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


def test_the_store_is_mongo_only_whatever_the_flags_say(monkeypatch):
    """Inverted on 2026-08-18, when the pgvector backend was deleted.

    This used to assert the opposite — that an empty flag map left the store
    writing Postgres. That default is gone: `embeddings` runs on Mongo alone,
    the backend predicates were already hardcoded, and the pg branches they
    guarded were unreachable before they were removed.

    Kept rather than deleted, because the guarantee it pins is the one that
    matters now: no flag state can route this store back to Postgres. An
    empty map is the strongest form of the question.
    """
    monkeypatch.setattr(mongo_store, "_BACKENDS", {})
    import ast
    import app.db.vector_store as vs_mod

    tree = ast.parse(Path(vs_mod.__file__).read_text(encoding="utf-8"))

    # By AST, not by substring: the module docstring mentions pgvector to
    # record that the backend was removed, and a text search cannot tell an
    # explanation from an import.
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    assert not {i for i in imported if "psycopg" in i or "pgvector" in i}
    assert "app.db.connection" not in imported
    assert "get_db" not in imported

    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "get_db" not in calls, "vector_store must not reach Postgres"
