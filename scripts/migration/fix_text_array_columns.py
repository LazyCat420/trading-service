#!/usr/bin/env python3
"""Reconcile the five Postgres `text[]` columns that Mongo holds in two shapes.

## The defect

Postgres declared exactly five `text[]` columns in the whole database
(`app/db/table_spec.py` counts them):

    morning_briefings.tickers_evaluated    flash_briefings.source_urls
    decision_scores.gates_failed           decision_scores.gates_unknown
    whiteboard_entries.edited_by

The PG->Mongo backfill wrote each one as the JSON **text** of the array
(`'["NVDA","DKS"]'`). Every writer that has run since the cutover hands pymongo
a real Python list, which lands as a BSON **array**. Nothing reconciled the
two, so each collection holds both shapes at once. A collection has no column
types, so nothing raised — measured 2026-08-24:

    decision_scores.gates_failed      string=312  array=83
    decision_scores.gates_unknown     string=312  array=83
    flash_briefings.source_urls       string=181  array=3
    morning_briefings.tickers_evaluated string=2  array=0
    whiteboard_entries.edited_by      string=1149 array=373

This is not cosmetic, because a JSON string is truthy and has a `.length`.
`morningBriefing.tickers_evaluated?.length > 0` passed on a 51-character
string and the `.map` on the next line threw
`tickers_evaluated.map is not a function` — the Live Feed widget's Morning tab
crashed to an error boundary. The Flash tab guarded with `Array.isArray(...)`
and so merely dropped the Sources list on all 181 migrated briefings, which is
the same defect wearing a quieter mask.

## Also stamped here: the defaults Postgres used to supply

`flash_briefings` was `id SERIAL PRIMARY KEY, created_at TIMESTAMP DEFAULT
CURRENT_TIMESTAMP`. Mongo has no column defaults and the writer was ported
across unchanged, so post-cutover briefings landed with neither field. The
reader sorts `created_at` descending, and a missing field ranks below every
real date in BSON type order, so those documents sort LAST and fall off the
limit: three briefings written on 2026-08-20 and 2026-08-22 were never once
displayed while the widget showed 2026-08-18 as "latest".

`app/services/flash_briefing.py` now stamps both on write. This backfills the
documents already written, taking `created_at` from the ObjectId's generation
time — which is the insert instant to the second, and is the only record of
when they were made.

Idempotent: a document already holding an array, or already stamped, is left
alone. Run with --apply to write; the default is a dry run.

    python scripts/migration/fix_text_array_columns.py           # dry run
    python scripts/migration/fix_text_array_columns.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

TEXT_ARRAY_COLUMNS = [
    ("decision_scores", "gates_failed"),
    ("decision_scores", "gates_unknown"),
    ("flash_briefings", "source_urls"),
    ("morning_briefings", "tickers_evaluated"),
    ("whiteboard_entries", "edited_by"),
]


def _parse(text: str) -> list | None:
    """The JSON text of a PG array -> a list. None if it is not that."""
    stripped = text.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def convert_arrays(coll_for, apply: bool) -> int:
    converted = 0
    for collection, field in TEXT_ARRAY_COLUMNS:
        col = coll_for(collection)
        # $type is evaluated per ELEMENT for an array field, so a plain
        # {field: {$type: 'string'}} filter also matches every array-of-strings
        # document. $expr compares the field's own type and does not.
        cursor = col.find(
            {"$expr": {"$eq": [{"$type": f"${field}"}, "string"]}},
            {field: 1},
        )
        unparseable = []
        n = 0
        for doc in cursor:
            value = _parse(doc[field])
            if value is None:
                unparseable.append(doc["_id"])
                continue
            if apply:
                col.update_one({"_id": doc["_id"]}, {"$set": {field: value}})
            n += 1
        converted += n
        note = f" ({len(unparseable)} left as text: not JSON)" if unparseable else ""
        print(f"  {collection}.{field}: {n} document(s) text -> array{note}")
    return converted


def stamp_flash_briefings(coll_for, apply: bool) -> int:
    """Give the post-cutover briefings the `id`/`created_at` PG used to supply."""
    col = coll_for("flash_briefings")
    current_max = 0
    for doc in col.find({"id": {"$exists": True}}, {"id": 1}).sort("id", -1).limit(1):
        current_max = int(doc["id"])

    orphans = list(col.find(
        {"$or": [{"id": {"$exists": False}}, {"created_at": {"$exists": False}}]},
        {"_id": 1, "id": 1, "created_at": 1},
    ).sort("_id", 1))

    for doc in orphans:
        update = {}
        if "created_at" not in doc:
            # The ObjectId carries the insert second. Stored naive-UTC to match
            # the seeded documents (app/db/date_fields normalises to that).
            update["created_at"] = doc["_id"].generation_time.astimezone(
                timezone.utc
            ).replace(tzinfo=None)
        if "id" not in doc:
            current_max += 1
            update["id"] = current_max
        if not update:
            continue
        print(f"  flash_briefings {doc['_id']}: stamping {update}")
        if apply:
            col.update_one({"_id": doc["_id"]}, {"$set": update})
    return len(orphans)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    args = ap.parse_args()

    # Resolve through mongo_store._coll, never db[name]: a table name is
    # mapped to its physical collection there (app/db/collection_map.json), and
    # a name that bypasses the map does not error — Mongo creates a new,
    # empty collection on first write.
    from app.db.mongo_store import _coll  # noqa: E402

    mode = "APPLY" if args.apply else "DRY RUN (pass --apply to write)"
    print(f"== fix_text_array_columns — {mode} ==")
    print("\ntext[] columns held as JSON text:")
    converted = convert_arrays(_coll, args.apply)
    print("\nflash_briefings missing the defaults Postgres used to supply:")
    stamped = stamp_flash_briefings(_coll, args.apply)
    print(f"\n{converted} array value(s), {stamped} briefing(s) "
          f"{'updated' if args.apply else 'would be updated'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
