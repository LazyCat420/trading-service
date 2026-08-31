"""A backfill from a FROZEN archive must be able to add without overwriting.

Postgres stopped taking writes on 2026-08-19 and Mongo has been the source of
truth since. `pg_to_mongo_backfill.py` had one write mode — `$set` from the
archive, upsert — which against a live collection is not a repair. For every
row the two stores share it is a REVERT of whatever has happened since.

That mattered the moment a real gap turned up. `technicals` is 37,842 rows
short of the archive (the backfill carried it from about 2024-09-09 forward and
nothing older; AAPL has 992 archive rows back to 1981 and 488 in Mongo). So it
needs the missing rows — and its rows are also recomputed in place by
`refresh_technicals.py`, so a blanket `$set` would undo any recomputation done
since the cutover. `--missing-only` is how you get the first without the second.

`$setOnInsert` is the whole mechanism: it writes the document when the natural
key is absent and does nothing at all when it is present.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pymongo
import pytest

from scripts.pg_to_mongo_backfill import _bulk_upsert, _key_filter


@pytest.fixture
def captured(monkeypatch):
    """Capture the bulk_write ops without touching a database."""
    ops_seen: list = []

    class _Result:
        upserted_count = 0
        upserted_ids: dict = {}

    coll = MagicMock()
    coll.bulk_write.side_effect = lambda ops, **kw: (ops_seen.extend(ops), _Result())[1]
    db = MagicMock()
    db.__getitem__.return_value = coll

    from app.db import mongo_store
    monkeypatch.setattr(mongo_store, "ensure_indexes", lambda *a, **k: None)
    monkeypatch.setattr(mongo_store, "get_doc_db", lambda: db)
    monkeypatch.setattr(mongo_store, "bulk_upsert",
                        lambda *a, **k: pytest.fail(
                            "missing_only must not route through the "
                            "single-key $set helper"))
    return ops_seen


DOC = {"ticker": "AAPL", "date": "1981-01-02", "rsi": 50}


def test_missing_only_uses_set_on_insert(captured):
    _bulk_upsert("technicals", [DOC], ["ticker", "date"], missing_only=True)
    assert len(captured) == 1
    op = captured[0]
    assert "$setOnInsert" in op._doc, op._doc
    assert "$set" not in op._doc, (
        "$set would overwrite a live document with the frozen archive's copy")
    assert op._upsert is True


def test_the_default_mode_is_still_a_plain_upsert(captured, monkeypatch):
    from app.db import mongo_store
    monkeypatch.setattr(mongo_store, "bulk_upsert", lambda *a, **k: 1)
    _bulk_upsert("technicals", [DOC], ["ticker", "date"], missing_only=False)
    assert "$set" in captured[0]._doc


def test_the_filter_is_the_natural_key_and_nothing_else(captured):
    _bulk_upsert("technicals", [DOC], ["ticker", "date"], missing_only=True)
    assert captured[0]._filter == {"ticker": "AAPL", "date": "1981-01-02"}
    assert _key_filter(["ticker", "date"], DOC) == captured[0]._filter


def test_missing_only_reports_only_what_it_inserted(monkeypatch):
    """The count must be upserted_count, not len(docs).

    Scanning 1.37M archive rows to insert 37,842 must not print "1,371,047
    inserted" — that number is what a blanket overwrite would have written, and
    reading it as the repair's size is how a revert gets mistaken for a fix.
    """
    class _Result:
        upserted_count = 2
        upserted_ids = {0: "id-a", 5: "id-b"}

    coll = MagicMock()
    coll.bulk_write.return_value = _Result()
    db = MagicMock()
    db.__getitem__.return_value = coll
    from app.db import mongo_store
    monkeypatch.setattr(mongo_store, "ensure_indexes", lambda *a, **k: None)
    monkeypatch.setattr(mongo_store, "get_doc_db", lambda: db)

    ids: list = []
    n = _bulk_upsert("technicals", [DOC] * 10, ["ticker", "date"],
                     missing_only=True, inserted_ids=ids)
    assert n == 2, "reported the batch size instead of the insert count"
    assert ids == ["id-a", "id-b"], "the rollback list must hold the new _ids"


def test_an_empty_batch_writes_nothing(captured):
    assert _bulk_upsert("technicals", [], ["ticker", "date"], missing_only=True) == 0
    assert captured == []
