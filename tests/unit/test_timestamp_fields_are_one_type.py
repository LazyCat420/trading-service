"""A timestamp column stores ONE type in Mongo, whichever way it is written.

THE DEFECT
----------
Same class as `test_date_fields_are_one_type`, found live the day after the
cutover (2026-08-20): the codemodded writers kept `.isoformat()` strings that
Postgres used to parse on the way in, so post-cutover rows landed as TEXT
beside the seeded BSON datetimes. BSON sorts Date above String, so every
"newest first" read kept answering the last PRE-cutover row, forever and
silently:

* `cycle_run_summaries` — the client's "audit the newest cycle" endpoint
  (`trading-client/app/routers/autoresearch.py`) and its own comment guards
  the null-sorts-first hazard while the TYPE hazard walked past it;
* `cycle_benchmarks` — the benchmarks panel pinned to a pre-cutover run;
* `episodic_memory` — the per-ticker recency sort could no longer reach ANY
  memory formed after the cutover (53/27/68 datetime rows outrank every new
  string row for AAPL/JPM/NVDA), i.e. the learning loop froze at the cutover
  and worsened each cycle.

And a string-dated row never matches a `$gte`/`$lte` datetime window at all.

So the seam contract now covers the manifest's `timestamp with/without time
zone` columns too, coerced by `as_timestamp`: ISO strings parse, everything
normalises to the store's native naive-UTC shape, and — the negative control
that separates it from `as_date` — the time of day is NEVER floored.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.db import date_fields


# ── the registry ───────────────────────────────────────────────────────────
def test_the_registry_comes_from_the_manifest_not_a_hand_list():
    assert date_fields.timestamp_fields("cycle_run_summaries") >= frozenset(
        {"started_at", "finished_at"})
    assert date_fields.timestamp_fields("cycle_benchmarks") >= frozenset(
        {"started_at", "finished_at"})
    assert "timestamp" in date_fields.timestamp_fields("episodic_memory")
    assert "run_at" in date_fields.timestamp_fields("consolidation_reports")


def test_an_unlisted_collection_is_untouched():
    """NEGATIVE CONTROL: per (collection, field), not per field name."""
    assert date_fields.timestamp_fields("no_such_collection") == frozenset()
    doc = {"created_at": "2026-08-20T03:23:14+00:00"}
    assert date_fields.coerce_doc("no_such_collection", doc) == doc


def test_date_and_timestamp_registries_do_not_bleed_into_each_other():
    """NEGATIVE CONTROL: `price_history.date` must still floor to midnight;
    a timestamp field must never be floored by the date path."""
    assert "date" in date_fields.date_fields("price_history")
    assert "date" not in date_fields.timestamp_fields("price_history")


# ── as_timestamp: the one representation ───────────────────────────────────
@pytest.mark.parametrize("value", [
    "2026-08-20T03:23:14.455175+00:00",
    "2026-08-20 03:23:14.455175+00:00",
    "2026-08-20T03:23:14.455175Z",
    datetime(2026, 8, 20, 3, 23, 14, 455175),
    datetime(2026, 8, 20, 3, 23, 14, 455175, tzinfo=timezone.utc),
])
def test_every_spelling_of_an_instant_converges(value):
    assert date_fields.as_timestamp(value) == datetime(2026, 8, 20, 3, 23, 14, 455175)


def test_the_result_is_naive_because_the_store_reads_back_naive():
    """BSON keeps UTC milliseconds; pymongo decodes without tzinfo. An aware
    value is converted THROUGH UTC, not stripped."""
    est = datetime(2026, 8, 19, 22, 23, 14, tzinfo=timezone.utc)
    out = date_fields.as_timestamp(est)
    assert out.tzinfo is None and out == datetime(2026, 8, 19, 22, 23, 14)


def test_the_time_of_day_is_never_floored():
    """NEGATIVE CONTROL vs `as_date`: flooring a timestamp destroys the value."""
    out = date_fields.as_timestamp("2026-08-20T03:23:14")
    assert (out.hour, out.minute, out.second) == (3, 23, 14)


def test_a_bare_day_becomes_midnight_and_a_date_object_encodes():
    """pymongo raises InvalidDocument on a bare `datetime.date`; midnight is
    the only faithful reading of a day handed to a timestamp column."""
    assert date_fields.as_timestamp("2026-08-20") == datetime(2026, 8, 20)
    assert date_fields.as_timestamp(date(2026, 8, 20)) == datetime(2026, 8, 20)


def test_a_value_it_cannot_read_is_returned_untouched():
    assert date_fields.as_timestamp("fifteen minutes ago") == "fifteen minutes ago"
    assert date_fields.as_timestamp(1787193855) == 1787193855
    assert date_fields.as_timestamp(None) is None


# ── the calls the audit caught writing text ────────────────────────────────
class _FakeCollection:
    def __init__(self):
        self.calls = []

    def update_one(self, key, update, **kw):
        self.calls.append(("update_one", key, update))

    def update_many(self, key, update, **kw):
        self.calls.append(("update_many", key, update))

        class _R:
            matched_count = 0
            modified_count = 0
        return _R()

    def insert_many(self, docs, **kw):
        self.calls.append(("insert_many", docs))

        class _R:
            inserted_ids = [None] * len(docs)
        return _R()

    def find(self, query, projection=None, **kw):
        self.calls.append(("find", query, projection))

        class _C:
            def sort(self, *a):
                return self

            def limit(self, *a):
                return self

            def __iter__(self):
                return iter(())
        return _C()


@pytest.fixture
def store(monkeypatch):
    from app.db import mongo_store

    coll = _FakeCollection()
    monkeypatch.setattr(mongo_store, "_coll", lambda table: coll)
    monkeypatch.setattr(mongo_store, "ensure_indexes", lambda session=None: None)
    return mongo_store, coll


def test_log_managers_cycle_summary_lands_as_datetimes(store):
    """The exact write that pinned the 'newest cycle' sort: `finished_at` in
    `$set` and `started_at` in `$setOnInsert`, both isoformat text."""
    mongo_store, coll = store
    mongo_store.update_docs("cycle_run_summaries", {"cycle_id": "c1"}, {
        "$set": {"finished_at": "2026-08-20T03:23:14.455175+00:00", "status": "done"},
        "$setOnInsert": {"started_at": "2026-08-20T02:44:15.684923+00:00"},
    })
    _, _, update = coll.calls[0]
    assert update["$set"]["finished_at"] == datetime(2026, 8, 20, 3, 23, 14, 455175)
    assert update["$set"]["status"] == "done"
    assert update["$setOnInsert"]["started_at"] == datetime(2026, 8, 20, 2, 44, 15, 684923)


def test_an_episodic_memory_is_retrievable_by_the_recency_sort(store):
    """The write that froze the learning loop: `timestamp` as isoformat text
    sorts below every seeded datetime and can never be recalled."""
    mongo_store, coll = store
    mongo_store.insert_docs("episodic_memory", [{
        "id": "m1", "ticker": "AAPL",
        "timestamp": "2026-08-20T03:23:14+00:00", "summary": "s",
    }])
    _, docs = coll.calls[0]
    assert docs[0]["timestamp"] == datetime(2026, 8, 20, 3, 23, 14)


def test_a_datetime_window_filter_reaches_string_written_history(store):
    mongo_store, coll = store
    mongo_store.find_docs("cycle_run_summaries",
                          {"started_at": {"$gte": "2026-08-20T00:00:00"}})
    _, query, _ = coll.calls[0]
    assert query["started_at"]["$gte"] == datetime(2026, 8, 20)


def test_ne_none_is_not_rewritten(store):
    """NEGATIVE CONTROL: the client's newest-cycle filter is
    `{"started_at": {"$ne": None}}` — None must pass through, and `$exists`
    style operands must never be touched."""
    mongo_store, coll = store
    mongo_store.find_docs("cycle_run_summaries", {"started_at": {"$ne": None}})
    _, query, _ = coll.calls[0]
    assert query == {"started_at": {"$ne": None}}
