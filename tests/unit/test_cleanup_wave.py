"""Cleanup-wave regression guards: zero-vector rejection, embedding dedup,
and sell-side trigger retirement under hard_stop exit ownership.

These patched `get_db` and asserted on captured SQL text. Both modules go
through the Mongo helpers now, so those patches raised AttributeError; the SQL
assertions are replaced with structural ones on the Mongo calls — collection,
filter, update — which pin more than the string ever did.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.db.vector_store import VectorStore
from app.trading import order_triggers as ot


SELL_SIDE = {
    'bot_id': 'bot-x',
    'ticker': 'DIS',
    'active': True,
    'trigger_type': {'$in': ['stop_loss', 'take_profit']},
}


def test_zero_vector_rejected_before_db():
    # `_mongo_store` imports mongo_store INSIDE the method, so the patch has to
    # land on the module itself — patching a VectorStore attribute is a no-op.
    with patch("app.db.mongo_store.delete_docs",
               side_effect=AssertionError("DB must not be touched")), \
         patch("app.db.mongo_store.update_docs",
               side_effect=AssertionError("DB must not be touched")):
        vs = VectorStore()
        assert vs.store_embedding("t", "id1", "AAPL", "x", [0.0] * 384) == ""
        assert vs.store_embedding("t", "id1", "AAPL", "x", []) == ""


def test_store_embedding_deletes_prior_rows_for_source():
    """One embedding per source row: the prior rows for this (table, id) go
    before the new one lands, or a re-embed leaves two vectors competing in
    cosine search."""
    calls = []
    with patch("app.db.mongo_store.delete_docs",
               side_effect=lambda *a, **k: calls.append(("delete", a, k))), \
         patch("app.db.mongo_store.update_docs",
               side_effect=lambda *a, **k: calls.append(("upsert", a, k))):
        vs = VectorStore()
        eid = vs.store_embedding("canonical_memories", "mem-1", "AAPL", "x", [0.1, 0.2])

    assert eid
    assert [c[0] for c in calls] == ["delete", "upsert"], (
        "the prior rows must be cleared BEFORE the new vector is written"
    )
    _, del_args, _ = calls[0]
    assert del_args[0] == "embeddings"
    # Scoped to this source row — a broader filter would wipe the collection.
    assert del_args[1] == {"source_table": "canonical_memories", "source_id": "mem-1"}

    _, up_args, up_kw = calls[1]
    assert up_args[0] == "embeddings"
    assert up_args[1] == {"id": eid}
    assert up_kw.get("upsert") is True
    doc = up_args[2]["$set"]
    assert doc["source_table"] == "canonical_memories"
    assert doc["source_id"] == "mem-1"
    assert doc["ticker"] == "AAPL"
    assert doc["dim"] == 2


def test_deactivate_sell_side_triggers_counts_retired():
    q = MagicMock()
    # find_rows returns TUPLES in the requested column order — ['id'] here.
    q.find_rows.side_effect = lambda coll, *a, **k: (
        [("id1",), ("id2",)] if coll == "price_triggers" else []
    )
    store = MagicMock()
    with patch.object(ot, "mongo_query", q), patch.object(ot, "mongo_store", store):
        assert ot.deactivate_sell_side_triggers("bot-x", "DIS") == 2

    # Only the sell side retires: a buy-side or already-inactive trigger caught
    # by this filter would be silently switched off with it.
    assert q.find_rows.call_args[0][0] == "price_triggers"
    assert q.find_rows.call_args[0][1] == SELL_SIDE
    store.update_docs.assert_called_once_with(
        "price_triggers", SELL_SIDE, {"$set": {"active": False}}
    )


def test_deactivate_sell_side_triggers_never_raises():
    q = MagicMock()
    q.find_rows.side_effect = RuntimeError("db down")
    store = MagicMock()
    with patch.object(ot, "mongo_query", q), patch.object(ot, "mongo_store", store):
        assert ot.deactivate_sell_side_triggers("bot-x", "DIS") == 0
    # A failed read must not be followed by a blind write.
    store.update_docs.assert_not_called()
