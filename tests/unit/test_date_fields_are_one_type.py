"""A date column stores ONE type in Mongo, whichever way the value is written.

THE DEFECT
----------
Postgres parsed a string into a `date` on the way in, so writing
`latest_date.strftime("%Y-%m-%d")` into a date column was correct for years.
Ported to Mongo the same line stores the STRING, beside the documents the
backfill wrote as naive-midnight datetimes — and nothing raises, because a
collection has no column types.

What that costs, measured on the live store (2,798 `sector_performance`
documents, every one of them a BSON date):

* the upsert key `{"sector": s, "date": "2026-08-18"}` matches no seeded
  document, so the cycle inserts a SECOND document for a day that exists;
* `max(date)` sorts by BSON type order, where Date outranks String — so the
  heatmap keeps returning the last backfilled day and never sees the new rows;
* a date-ordered series gets every string before every date.

So the contract is enforced at the seam every read and write passes through
(`mongo_store`), for exactly the (collection, field) pairs the schema manifest
declares `date` — 24 columns across 19 tables.

The negative controls matter as much as the assertions: a coercion that fires
too widely rewrites timestamps, range bounds and unrelated string fields, and
the symptom of THAT is a query that quietly matches nothing.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.db import date_fields


# ── the registry ───────────────────────────────────────────────────────────
def test_the_registry_comes_from_the_manifest_not_a_hand_list():
    """19 tables / 24 columns, as `schema_manifest.json` declares them."""
    assert date_fields.date_fields("sector_performance") == frozenset({"date"})
    assert date_fields.date_fields("price_history") == frozenset({"date"})
    assert date_fields.date_fields("fundamentals") == frozenset(
        {"earnings_date", "ipo_date", "snapshot_date"})
    assert sum(len(f) for f in date_fields.DATE_FIELDS.values()) == 24


def test_a_collection_with_no_date_column_is_untouched():
    """NEGATIVE CONTROL: the coercion is per (collection, field), not per name.

    `analysis_results.created_at` is a timestamp and `news_articles` has no
    date column at all; a registry that matched on the field NAME would floor
    both to midnight and destroy the time of day.
    """
    assert date_fields.date_fields("analysis_results") == frozenset()
    doc = {"date": "2026-08-18", "created_at": datetime(2026, 8, 18, 14, 5)}
    assert date_fields.coerce_doc("analysis_results", doc) == doc
    assert date_fields.coerce_filter("analysis_results", {"date": "2026-08-18"}) \
        == {"date": "2026-08-18"}


# ── as_date: the one representation ────────────────────────────────────────
@pytest.mark.parametrize("value", [
    "2026-08-18",
    date(2026, 8, 18),
    datetime(2026, 8, 18),
    datetime(2026, 8, 18, 23, 59, 59, 999000),
    datetime(2026, 8, 18, 4, 30, tzinfo=timezone.utc),
])
def test_every_spelling_of_a_day_writes_the_same_document(value):
    assert date_fields.as_date(value) == datetime(2026, 8, 18)


def test_the_stored_value_is_naive_because_the_backfill_stored_naive():
    """`table_spec._coerce` wrote `datetime(y, m, d)` — no tzinfo. A tz-aware
    midnight is a DIFFERENT BSON value only in Python's eyes, but it is a
    different equality test here, and the natural keys are built on it."""
    assert date_fields.as_date("2026-08-18").tzinfo is None


def test_a_value_it_cannot_read_is_returned_untouched():
    """A quiet substitution would be worse than the mismatch downstream."""
    assert date_fields.as_date("last thursday") == "last thursday"
    assert date_fields.as_date(20260818) == 20260818
    assert date_fields.as_date(None) is None


def test_a_string_that_is_not_a_plain_day_is_left_alone():
    """NEGATIVE CONTROL: only `YYYY-MM-DD` exactly. An ISO timestamp string in
    a date field is a different defect and must not be laundered into a
    plausible midnight."""
    assert date_fields.as_date("2026-08-18T14:05:00") == "2026-08-18T14:05:00"
    assert date_fields.as_date("2026-8-18") == "2026-8-18"


# ── documents ──────────────────────────────────────────────────────────────
def test_a_written_document_is_floored_to_midnight():
    doc = date_fields.coerce_doc("sector_performance", {
        "sector": "Energy", "date": "2026-08-18", "avg_return_1d": 1.5,
    })
    assert doc["date"] == datetime(2026, 8, 18)
    assert doc["sector"] == "Energy" and doc["avg_return_1d"] == 1.5


def test_the_upsert_key_is_coerced_too_or_it_matches_nothing():
    """The defect was in the KEY as much as the document: a string-dated filter
    against date-typed documents inserts a duplicate instead of updating."""
    key = date_fields.coerce_filter(
        "sector_performance", {"sector": "Energy", "date": "2026-08-18"})
    assert key == {"sector": "Energy", "date": datetime(2026, 8, 18)}


# ── filters ────────────────────────────────────────────────────────────────
def test_operators_that_take_a_value_are_coerced():
    q = date_fields.coerce_filter("price_history", {
        "ticker": "AAPL",
        "date": {"$gte": "2026-08-01", "$lt": date(2026, 9, 1)},
    })
    assert q["date"] == {"$gte": datetime(2026, 8, 1), "$lt": datetime(2026, 9, 1)}
    assert q["ticker"] == "AAPL"


def test_in_and_nin_coerce_each_element():
    q = date_fields.coerce_filter(
        "price_history", {"date": {"$in": ["2026-08-17", date(2026, 8, 18)]}})
    assert q["date"]["$in"] == [datetime(2026, 8, 17), datetime(2026, 8, 18)]


def test_operators_whose_operand_is_not_a_date_are_not_rewritten():
    """NEGATIVE CONTROL: `$exists: True` and `$type: "date"` take a bool and a
    type name. A blanket rewrite of every operand would turn `$exists` into a
    literal and the filter would match nothing — the exact silent-empty failure
    this module exists to prevent."""
    q = date_fields.coerce_filter(
        "price_history", {"date": {"$exists": True, "$type": "date"}})
    assert q["date"] == {"$exists": True, "$type": "date"}


def test_a_datetime_range_bound_keeps_its_time():
    """NEGATIVE CONTROL: flooring a read bound to midnight silently WIDENS the
    window by up to a day. Only writes are floored; see the module docstring."""
    cutoff = datetime(2026, 8, 18, 14, 5, 30)
    q = date_fields.coerce_filter("price_history", {"date": {"$gte": cutoff}})
    assert q["date"]["$gte"] == cutoff


def test_and_or_nor_are_walked():
    q = date_fields.coerce_filter("price_history", {
        "$or": [{"date": "2026-08-18"}, {"date": {"$lt": "2026-01-01"}}],
    })
    assert q["$or"][0]["date"] == datetime(2026, 8, 18)
    assert q["$or"][1]["date"]["$lt"] == datetime(2026, 1, 1)


# ── updates and pipelines ──────────────────────────────────────────────────
def test_set_and_setoninsert_are_coerced_but_inc_is_not():
    u = date_fields.coerce_update("price_history", {
        "$set": {"date": "2026-08-18", "close": 1.0},
        "$inc": {"volume": 5},
    })
    assert u["$set"]["date"] == datetime(2026, 8, 18)
    assert u["$inc"] == {"volume": 5}


def test_the_leading_match_stage_is_coerced():
    pipe = date_fields.coerce_pipeline("price_history", [
        {"$match": {"date": "2026-08-18"}},
        {"$group": {"_id": "$ticker", "n": {"$sum": 1}}},
    ])
    assert pipe[0] == {"$match": {"date": datetime(2026, 8, 18)}}
    assert pipe[1] == {"$group": {"_id": "$ticker", "n": {"$sum": 1}}}


def test_a_later_match_is_left_alone():
    """NEGATIVE CONTROL: after a `$group`, `date` is whatever the pipeline
    named — not necessarily this collection's date column."""
    pipe = [{"$group": {"_id": "$ticker", "date": {"$max": "$date"}}},
            {"$match": {"date": "2026-08-18"}}]
    assert date_fields.coerce_pipeline("price_history", pipe) == pipe


# ── the seam ───────────────────────────────────────────────────────────────
class _FakeCollection:
    def __init__(self):
        self.calls = []

    def update_one(self, key, update, **kw):
        self.calls.append(("update_one", key, update))

    def find(self, query, projection=None, **kw):
        self.calls.append(("find", query, projection))
        return _FakeCursor()

    def bulk_write(self, ops, **kw):
        self.calls.append(("bulk_write", ops))

    def count_documents(self, query, **kw):
        self.calls.append(("count", query))
        return 0


class _FakeCursor:
    def sort(self, *a):
        return self

    def limit(self, *a):
        return self

    def __iter__(self):
        return iter(())


@pytest.fixture
def store(monkeypatch):
    """`mongo_store` with the collection and the index build stubbed out."""
    from app.db import mongo_store

    coll = _FakeCollection()
    monkeypatch.setattr(mongo_store, "_coll", lambda table: coll)
    monkeypatch.setattr(mongo_store, "ensure_indexes", lambda session=None: None)
    return mongo_store, coll


def test_the_store_writes_a_date_even_when_the_caller_hands_it_a_string(store):
    """END TO END, at the seam: this is the call `sector_aggregator` makes."""
    mongo_store, coll = store
    mongo_store.upsert_doc(
        "sector_performance",
        {"sector": "Energy", "date": "2026-08-18"},
        {"sector": "Energy", "date": "2026-08-18", "avg_return_1d": 1.5},
    )
    _, key, update = coll.calls[0]
    assert key["date"] == datetime(2026, 8, 18)
    assert update["$set"]["date"] == datetime(2026, 8, 18)


def test_the_store_reads_a_date_even_when_the_caller_hands_it_a_string(store):
    mongo_store, coll = store
    mongo_store.find_docs("price_history", {"ticker": "AAPL", "date": "2026-08-18"})
    _, query, _ = coll.calls[0]
    assert query == {"ticker": "AAPL", "date": datetime(2026, 8, 18)}


def test_a_date_object_reaches_the_driver_as_a_datetime(store):
    """pymongo raises InvalidDocument on a bare `datetime.date` — that is the
    ERROR the translation-parity sweep scored against `sector_aggregator` on
    2026-08-17 (`cannot encode object: datetime.date(2025, 5, 7)`). It was the
    store's to absorb, not the caller's to remember."""
    mongo_store, coll = store
    mongo_store.count_docs("price_history", {"date": date(2025, 5, 7)})
    _, query = coll.calls[0]
    assert query == {"date": datetime(2025, 5, 7)}


def test_bulk_upsert_coerces_every_document(store):
    mongo_store, coll = store
    mongo_store.bulk_upsert(
        "price_history",
        [{"ticker": "AAPL", "date": "2026-08-18", "source": "yfinance", "close": 1.0},
         {"ticker": "MSFT", "date": date(2026, 8, 18), "source": "yfinance", "close": 2.0}],
        key_field=("ticker", "date", "source"),
    )
    _, ops = coll.calls[0]
    for op in ops:
        assert op._filter["date"] == datetime(2026, 8, 18)
        assert op._doc["$set"]["date"] == datetime(2026, 8, 18)


def test_an_unregistered_collection_passes_through_the_seam_unchanged(store):
    """NEGATIVE CONTROL at the seam: 300-odd call sites go through these
    helpers, and all but 19 tables must come out byte-identical."""
    mongo_store, coll = store
    doc = {"cycle_id": "c1", "date": "2026-08-18", "created_at": datetime(2026, 8, 18, 9, 1)}
    mongo_store.upsert_doc("analysis_results", {"cycle_id": "c1"}, doc)
    _, key, update = coll.calls[0]
    assert key == {"cycle_id": "c1"}
    assert update["$set"] == doc
