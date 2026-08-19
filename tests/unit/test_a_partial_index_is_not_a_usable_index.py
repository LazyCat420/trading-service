"""A partial index does not serve `{"id": <value>}`, and the guard counted it.

THE DEFECT
----------
`ensure_indexes` builds each migrated collection's natural key as

    create_index("id", unique=True,
                 partialFilterExpression={"id": {"$type": [...]}})

MongoDB will only CONSIDER a partial index when the query predicate is provably
a subset of that filter, and `{"id": "err_0000..."}` is not a subset of
`{"id": {"$type": [...]}}` — the planner does not infer type membership from a
literal. So every keyed read and every upsert filter on those collections plans
a COLLSCAN.

`ensure_key_index` exists to prevent exactly this ("without an index on that key
every single upsert is a COLLECTION SCAN... price_history would never have
finished"). It compared index KEY SHAPES, saw `("id",)` from the partial index,
reported "already present", and skipped building the one index that would have
been used. **The guard passed its own check while the thing it guards against
was happening.**

MEASURED 2026-08-19, live
-------------------------
`explain` on `{"id": <a real id>}`:

    execution_errors (173,005 docs, partial-only)  ->  COLLSCAN
    congress_trades  (has a plain `natural_key`)   ->  IXSCAN natural_key

and the seed rates line up exactly with which collections carried a plain index
beside the partial one:

    congress_trades       7,092 rows/s     execution_errors     ~33 rows/s
    agent_audit_log       5,930 rows/s     news_articles          23 rows/s
    macro_indicators      6,629 rows/s     context_blobs          37 rows/s

Document size does not explain it: congress_trades averages 313 bytes and
execution_errors 405.

Both halves are fixed — `ensure_indexes` now builds a plain `id_plain_1` beside
the partial-unique one, and `ensure_key_index` no longer counts a partial index
as usable.
"""
from __future__ import annotations

import pytest

from app.db import mongo_store


class _FakeCollection:
    def __init__(self, indexes):
        self._indexes = indexes
        self.created: list[tuple] = []

    def list_indexes(self):
        return iter(self._indexes)

    def create_index(self, keys, **kwargs):
        self.created.append((keys, kwargs))
        return kwargs.get("name", "idx")


PARTIAL_ONLY = [
    {"key": {"_id": 1}, "name": "_id_"},
    {"key": {"id": 1}, "name": "id_1", "unique": True,
     "partialFilterExpression": {"id": {"$type": ["string", "int", "long", "double"]}}},
]
PARTIAL_PLUS_PLAIN = PARTIAL_ONLY + [{"key": {"id": 1}, "name": "id_plain_1"}]


@pytest.fixture
def backfill(monkeypatch):
    import importlib

    mod = importlib.import_module("scripts.pg_to_mongo_backfill")
    return mod


def _with_indexes(monkeypatch, mod, indexes):
    coll = _FakeCollection(indexes)
    monkeypatch.setattr(mod.mongo_store, "get_doc_db", lambda: {"execution_errors": coll})
    monkeypatch.setattr(mod, "collection_for", lambda t: t)
    return coll


def test_a_partial_index_alone_does_not_satisfy_the_guard(backfill, monkeypatch):
    """THE FIX. Before this, the guard returned 'already present' here and the
    173,005-document collection was seeded through a collection scan."""
    coll = _with_indexes(monkeypatch, backfill, PARTIAL_ONLY)
    msg = backfill.ensure_key_index("execution_errors", ["id"])
    assert "created" in msg, msg
    assert coll.created, "no index was built"
    keys, kwargs = coll.created[0]
    assert keys == [("id", 1)]
    assert "partialFilterExpression" not in kwargs


def test_a_plain_index_beside_it_does_satisfy_the_guard(backfill, monkeypatch):
    """NEGATIVE CONTROL: the guard must not rebuild an index that already works,
    on every table, on every run. `congress_trades` and `agent_audit_log` are
    the live examples of this shape."""
    coll = _with_indexes(monkeypatch, backfill, PARTIAL_PLUS_PLAIN)
    msg = backfill.ensure_key_index("execution_errors", ["id"])
    assert "already present" in msg, msg
    assert coll.created == []


def test_a_composite_key_is_still_matched_by_shape(backfill, monkeypatch):
    """price_history keys on (ticker, date, source). The partial-index rule must
    not break the composite case, which is 26 of the migrated tables."""
    composite = [
        {"key": {"_id": 1}, "name": "_id_"},
        {"key": {"ticker": 1, "date": 1, "source": 1}, "name": "natural_key"},
    ]
    coll = _with_indexes(monkeypatch, backfill, composite)
    msg = backfill.ensure_key_index("execution_errors", ["ticker", "date", "source"])
    assert "already present" in msg
    assert coll.created == []


def test_every_partial_unique_collection_also_declares_a_plain_index():
    """The other half: `ensure_indexes` has to BUILD the usable one.

    Asserted against the source rather than a live Mongo so it runs in the
    normal suite — the collections list is the same one the loop iterates, so a
    new entry cannot quietly arrive without its plain index.
    """
    import inspect

    src = inspect.getsource(mongo_store.ensure_indexes)
    assert 'partialFilterExpression={"id": {"$type": _ID_TYPES}}' in src
    assert '_try(coll, "id", name="id_plain_1")' in src
    # The two hand-written partial indexes outside the loop need the same pair.
    assert '_try("agent_audit_log", "request_id", name="request_id_plain_1")' in src
    assert '_try("context_blobs", "context_hash", name="context_hash_plain_1")' in src


def test_the_partial_and_plain_indexes_must_have_different_names():
    """`create_index` on the same key with different options is an ERROR, not a
    modification — so the plain one needs its own name or it never gets built,
    and the failure is swallowed by `_try`'s non-fatal logging."""
    import inspect

    src = inspect.getsource(mongo_store.ensure_indexes)
    assert 'name="id_plain_1"' in src
