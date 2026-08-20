"""The fields that hold a calendar DATE, and the one representation they take.

THE DEFECT THIS EXISTS FOR
--------------------------
Postgres had a `date` type. BSON does not — the closest thing is a datetime, so
`app/db/table_spec.py::_coerce` stores a PG `date` as **naive midnight UTC**,
and every one of the 24 date columns arrived in Mongo that way. That part was
handled.

What was not handled is the WRITERS. Under Postgres a date column happily
accepted a string: `INSERT ... VALUES ('2026-08-18')` and PG parsed it into a
date on the way in, so `sector_aggregator` and `market_regime_engine` both
wrote `latest_date.strftime("%Y-%m-%d")` and nothing was ever wrong. Ported to
Mongo the same line stores the STRING, beside 2,798 documents holding real
BSON dates — and Mongo does not complain, because a collection has no column
types.

The result is not an error, it is a split store:

* `upsert_doc("sector_performance", {"sector": s, "date": "2026-08-18"}, ...)`
  matches none of the seeded documents, so it INSERTS a second, string-dated
  document for a day that already exists.
* `agg_row("sector_performance", {}, [("max", "date")])` sorts by BSON type
  order, where Date ranks above String — so `max(date)` keeps returning the
  last BACKFILLED day and the dashboard freezes on it, while the cycle happily
  writes new rows that nothing can see.
* `sector_correlation_engine` sorts the series by date and gets every string
  before every date, i.e. a correctly-ordered series of the wrong shape.

None of that raises, and no count check catches it: the documents are all
there. So the fix is not a bug fix at three call sites — it is a type contract
enforced at the one seam every read and write already passes through
(`mongo_store`), for exactly the (collection, field) pairs that Postgres
declared `date`.

THE SOURCE OF TRUTH
-------------------
`app/db/schema_manifest.json`, which is generated from the live schema and
checked in — the same artifact `table_spec` derives its specs from. It is used
rather than a hand-written list because a hand-written list is a place for the
registry to drift from the tables; it is used rather than a live
`information_schema` query because Postgres is being retired and this contract
has to outlive it.

WHAT IS DELIBERATELY NOT COERCED
--------------------------------
A `datetime` in a READ filter is left exactly as written: `{"date": {"$gte":
cutoff}}` where cutoff carries a time is a legitimate range bound, and rounding
it to midnight would silently widen the window. Only write-side documents are
floored to midnight, which is what the column meant and what the backfill
stored.

TIMESTAMPS TOO (2026-08-20)
---------------------------
The same defect surfaced on the TIMESTAMP columns the day after the cutover:
`log_manager` wrote `cycle_run_summaries.finished_at` as `.isoformat()` text
beside 352 seeded BSON datetimes, and because Date outranks String in BSON
type order, every `sort=[("started_at", -1)]` kept answering the last
PRE-cutover cycle — the client's "audit the newest cycle" endpoint, the
benchmarks panel, and (worst) `episodic_memory`'s per-ticker recency sort,
which made every memory formed after the cutover unretrievable. Nothing
raised; the store just split. So the registry now also covers the manifest's
`timestamp with/without time zone` columns, coerced by `as_timestamp` — which
parses ISO strings and normalises to the store's native shape (naive UTC) but
never floors, because a timestamp's time of day is the value.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_MANIFEST = Path(__file__).with_name("schema_manifest.json")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Mongo query operators whose operand is a VALUE (or list of values) of the
# field's own type, so a date belongs inside them. `$exists`/`$type`/`$size`
# take a bool/type-name/int instead and must never be rewritten.
_VALUE_OPERATORS = frozenset({
    "$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin",
})

# Operators that take a LIST OF SUB-FILTERS, each of which is a filter document
# over the same collection and therefore has to be walked too.
_FILTER_LIST_OPERATORS = frozenset({"$and", "$or", "$nor"})

# Update operators whose operand is a document of field → value. `$inc`/`$mul`
# take numbers and `$unset` takes a throwaway, so none of them can hold a date.
_UPDATE_DOC_OPERATORS = frozenset({"$set", "$setOnInsert", "$max", "$min"})

_TIMESTAMP_TYPES = frozenset({
    "timestamp with time zone", "timestamp without time zone",
})


def _load() -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    columns = json.loads(_MANIFEST.read_text())["columns"]
    dates: dict[str, frozenset[str]] = {}
    stamps: dict[str, frozenset[str]] = {}
    for table, cols in columns.items():
        d = frozenset(c["name"] for c in cols if c.get("type") == "date")
        t = frozenset(c["name"] for c in cols if c.get("type") in _TIMESTAMP_TYPES)
        if d:
            dates[table] = d
        if t:
            stamps[table] = t
    return dates, stamps


DATE_FIELDS: dict[str, frozenset[str]]
TIMESTAMP_FIELDS: dict[str, frozenset[str]]
DATE_FIELDS, TIMESTAMP_FIELDS = _load()


def date_fields(collection: str) -> frozenset[str]:
    """The date-typed fields of `collection` (empty for anything unlisted)."""
    return DATE_FIELDS.get(collection, frozenset())


def timestamp_fields(collection: str) -> frozenset[str]:
    """The timestamp-typed fields of `collection` (empty for anything unlisted)."""
    return TIMESTAMP_FIELDS.get(collection, frozenset())


def as_date(value: Any) -> Any:
    """One calendar day as Mongo stores it: naive midnight UTC.

    Mirrors `table_spec._coerce`, which is what put the seeded documents in the
    store. A value this cannot read is returned untouched — a loud mismatch
    downstream beats a quiet substitution here.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        # Rebuilt rather than `.replace()`d so that a datetime SUBCLASS — a
        # pandas Timestamp, which the aggregators hand in and which carries
        # nanoseconds — becomes a plain datetime. Both encode to the same BSON,
        # but only one of them compares equal to what the backfill stored.
        return datetime(value.year, value.month, value.day)
    # NOTE the order: datetime SUBCLASSES date, so this must come second.
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and _ISO_DATE.match(value):
        return datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    return value


def as_timestamp(value: Any) -> Any:
    """A point in time as the store holds it: a plain datetime, naive, UTC.

    The backfill's rows read back naive-UTC (BSON keeps UTC milliseconds and
    pymongo decodes without tzinfo), so that is the one shape writes converge
    on. Unlike `as_date` this NEVER floors — a timestamp's time of day is the
    value. A value it cannot read is returned untouched: a loud mismatch
    downstream beats a quiet substitution here.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        # Rebuilt (not `.replace()`d) so a datetime SUBCLASS — a pandas
        # Timestamp — becomes the plain datetime the seeded rows compare to.
        return datetime(value.year, value.month, value.day, value.hour,
                        value.minute, value.second, value.microsecond)
    # NOTE the order: datetime subclasses date, so this must come second.
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return value


def _as_filter_value(value: Any) -> Any:
    """A date for a FILTER: strings and `date`s become datetimes, but a
    `datetime` keeps its time (see the module docstring — a range bound is not
    a typo)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return datetime(value.year, value.month, value.day, value.hour,
                        value.minute, value.second, value.microsecond)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and _ISO_DATE.match(value):
        return datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    return value


def _coerce_operand(operand: Any, coerce) -> Any:
    if isinstance(operand, dict):
        # {"$gte": x, "$lt": y} — rewrite only the operators that take a value.
        return {
            op: ([coerce(v) for v in val] if op in ("$in", "$nin") and isinstance(val, list)
                 else coerce(val) if op in _VALUE_OPERATORS
                 else val)
            for op, val in operand.items()
        }
    if isinstance(operand, list):
        return [coerce(v) for v in operand]
    return coerce(operand)


def coerce_filter(collection: str, query: Any) -> Any:
    """A read/match filter with this collection's date and timestamp fields
    normalised."""
    dfields = date_fields(collection)
    tfields = timestamp_fields(collection)
    if not (dfields or tfields) or not isinstance(query, dict):
        return query
    out = {}
    for key, val in query.items():
        if key in _FILTER_LIST_OPERATORS and isinstance(val, list):
            out[key] = [coerce_filter(collection, sub) for sub in val]
        elif key in dfields:
            out[key] = _coerce_operand(val, _as_filter_value)
        elif key in tfields:
            out[key] = _coerce_operand(val, as_timestamp)
        else:
            out[key] = val
    return out


def coerce_doc(collection: str, doc: Any) -> Any:
    """A document being written: date fields floored to midnight — the form
    the backfill stored and the natural keys are built on — and timestamp
    fields parsed to plain datetimes with their time kept."""
    dfields = date_fields(collection)
    tfields = timestamp_fields(collection)
    if not (dfields or tfields) or not isinstance(doc, dict):
        return doc
    return {
        k: (as_date(v) if k in dfields
            else as_timestamp(v) if k in tfields
            else v)
        for k, v in doc.items()
    }


def coerce_docs(collection: str, docs: list) -> list:
    return [coerce_doc(collection, d) for d in docs]


def coerce_update(collection: str, update: Any) -> Any:
    """An update document — `{"$set": {...}}` and friends walked one level in.

    `$inc`/`$unset`/`$push` are left alone: none of them can carry a calendar
    date for a date column, and rewriting an operand this does not understand
    is how a helper turns a working query into a silent no-match.
    """
    if not isinstance(update, dict) or not (
            date_fields(collection) or timestamp_fields(collection)):
        return update
    return {
        op: (coerce_doc(collection, val) if op in _UPDATE_DOC_OPERATORS else val)
        for op, val in update.items()
    }


def coerce_pipeline(collection: str, pipeline: Any) -> Any:
    """An aggregation pipeline: the LEADING `$match` stage only.

    That stage is an ordinary filter over the collection's own fields, and it
    is where every translated GROUP BY puts its WHERE. Anything after the first
    stage computes over names the pipeline itself invented (`_id`, aliases), so
    a `date` there need not be this collection's `date` — rewriting it would be
    guesswork.
    """
    if not isinstance(pipeline, list) or not pipeline or not (
            date_fields(collection) or timestamp_fields(collection)):
        return pipeline
    head = pipeline[0]
    if not (isinstance(head, dict) and set(head) == {"$match"}):
        return pipeline
    return [{"$match": coerce_filter(collection, head["$match"])}, *pipeline[1:]]
