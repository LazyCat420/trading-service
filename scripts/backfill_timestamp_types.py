#!/usr/bin/env python3
"""Converge every timestamp column on one BSON type: datetime.

WHY THIS EXISTS
---------------
Between the cutover (2026-08-19 02:31Z) and the date_fields timestamp
contract, the codemodded writers stored `.isoformat()` TEXT in columns whose
seeded rows are BSON datetimes. Date outranks String in BSON type order, so
every `sort -1` on those fields kept answering the last pre-cutover row —
the client's newest-cycle endpoint, the benchmarks panel, and
`episodic_memory`'s recency sort (which made post-cutover memories
unretrievable). The seam fix stops NEW text; this script repairs what
already landed, one `$type: "string"` filter per (collection, field) from
the same registry the seam enforces (`app/db/date_fields.TIMESTAMP_FIELDS`).

USAGE
-----
    .venv/bin/python scripts/backfill_timestamp_types.py            # census only
    .venv/bin/python scripts/backfill_timestamp_types.py --apply    # convert
    .venv/bin/python scripts/backfill_timestamp_types.py --verify   # assert zero left

The census is the default because a repair script that mutates on a bare
invocation is how a probe becomes an incident. `--apply` refuses to run
until it has printed what it is about to touch, and skips (and names) any
string `as_timestamp` cannot parse rather than laundering it.

Back up first: `mongodump` the collections the census names before --apply.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.date_fields import TIMESTAMP_FIELDS, as_timestamp  # noqa: E402
from app.db.mongo import get_mongo_client  # noqa: E402
import os  # noqa: E402

# Collections that are a frozen-archive concern or too big to collscan
# casually get named, not silently skipped (memory: no silent caps).
BIG_SCAN_WARN_DOCS = 2_000_000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="convert string rows")
    ap.add_argument("--verify", action="store_true", help="exit 1 if any string remains")
    args = ap.parse_args()

    db = get_mongo_client()[os.getenv("TRADING_MONGO_DB", "trading_bot")]
    existing = set(db.list_collection_names())

    found: list[tuple[str, str, int]] = []
    unparseable: list[tuple[str, str, str]] = []
    converted = 0

    for coll in sorted(TIMESTAMP_FIELDS):
        if coll not in existing:
            continue
        est = db[coll].estimated_document_count()
        if est > BIG_SCAN_WARN_DOCS:
            print(f"[warn] {coll}: {est:,} docs — this census collscans it", file=sys.stderr)
        for field in sorted(TIMESTAMP_FIELDS[coll]):
            n = db[coll].count_documents({field: {"$type": "string"}})
            if not n:
                continue
            found.append((coll, field, n))
            print(f"{coll}.{field}: {n} string-typed")
            if not args.apply:
                continue
            for doc in db[coll].find({field: {"$type": "string"}}, {field: 1}):
                parsed = as_timestamp(doc[field])
                if isinstance(parsed, str):
                    unparseable.append((coll, field, doc[field][:40]))
                    continue
                db[coll].update_one({"_id": doc["_id"]}, {"$set": {field: parsed}})
                converted += 1

    if not found:
        print("clean: no string-typed values in any registered timestamp field")
    if args.apply:
        print(f"converted {converted} values")
        for coll, field, val in unparseable:
            print(f"[skipped, not ISO] {coll}.{field}: {val!r}")
    if args.verify and found:
        print("VERIFY FAILED: string-typed timestamps remain", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
