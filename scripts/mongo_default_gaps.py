#!/usr/bin/env python3
"""Which values did the Mongo write path stop supplying when Postgres left?

Postgres filled a column DEFAULT in on INSERT. Mongo fills in nothing. A writer
converted by the codemod listed the columns the SQL named — and the SQL never
named the defaulted ones, because the database was supplying them. So they
disappeared, without an error, from every document written after the cutover.

That is not cosmetic. A missing field does not compare:

    {"rate_limited_count": {"$lt": 5}}   matches neither a null nor a missing
                                         field, so a ticker that has never been
                                         rate-limited is never retried
    {"status": "pending"}                a status-less command is never polled
    sort=[("created_at", -1)]            a stamp-less document sorts as if it
                                         had no time at all

Measured 2026-08-30 this found 38 (collection, column) pairs across 23
collections whose POST-CUTOVER documents lack a value the archive always had —
including the two examples above, both of which were live and silent.

Needs no Postgres: the defaults come from
`docs/migration/pg_column_defaults.json`, so this keeps working after the
archive is closed. Regenerate that artifact with
`scripts/export_pg_column_defaults.py`.

    python scripts/mongo_default_gaps.py
    python scripts/mongo_default_gaps.py --all        # include pre-cutover docs
    python scripts/mongo_default_gaps.py --json reports/default_gaps.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
ARTIFACT = REPO / "docs" / "migration" / "pg_column_defaults.json"

#: The moment Postgres stopped taking writes. Documents older than this came
#: from the backfill and inherit whatever the archive row had, so a gap there
#: is the archive's, not the writer's.
CUTOVER = datetime.datetime(2026, 8, 19, tzinfo=datetime.timezone.utc)


def load_defaults(path: Path = ARTIFACT) -> dict[str, dict[str, str]]:
    return json.loads(path.read_text())["tables"]


def scan(*, post_cutover_only: bool = True, timeout_ms: int = 30000,
         db=None, defaults: dict | None = None) -> list[dict]:
    """One row per (collection, column) whose documents lack the value.

    `db` and `defaults` are injectable so the classification can be tested
    without a live store — a rule this instrument's own subject taught: a
    checker nobody can run offline is a checker nobody runs.
    """
    from bson import ObjectId

    from app.db.collections import collection_for

    if db is None:
        from app.db.mongo_store import get_doc_db
        db = get_doc_db()
    live = set(db.list_collection_names())
    era = ({"_id": {"$gt": ObjectId.from_datetime(CUTOVER)}}
           if post_cutover_only else {})

    out: list[dict] = []
    for table, columns in sorted((defaults or load_defaults()).items()):
        try:
            coll = collection_for(table)
        except Exception:
            coll = table
        if coll not in live:
            continue
        population = db[coll].count_documents(dict(era), maxTimeMS=timeout_ms)
        if not population:
            continue
        for column, default in sorted(columns.items()):
            missing = db[coll].count_documents(
                {**era, column: {"$exists": False}}, maxTimeMS=timeout_ms)
            if missing:
                out.append({
                    "collection": coll, "table": table, "column": column,
                    "default": default, "population": population,
                    "missing": missing,
                    "share": round(missing / population, 4),
                })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="count every document, not only post-cutover ones")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--collection", action="append",
                    help="restrict the report to these collections")
    args = ap.parse_args()

    rows = scan(post_cutover_only=not args.all)
    if args.collection:
        rows = [r for r in rows if r["collection"] in args.collection]

    era = "every document" if args.all else "documents written after the cutover"
    print(f"Values the archive supplied and the Mongo writer does not — {era}\n")
    if not rows:
        print("  none. Every defaulted column is present on every document counted.")
    else:
        print(f"  {'collection':32s} {'column':24s} {'missing':>8s} {'of':>8s}  default")
        print("  " + "-" * 96)
        for r in rows:
            print(f"  {r['collection']:32s} {r['column']:24s} "
                  f"{r['missing']:>8d} {r['population']:>8d}  {(r['default'] or '')[:28]}")
        cols = len({(r['collection'], r['column']) for r in rows})
        print(f"\n  {cols} (collection, column) pairs across "
              f"{len({r['collection'] for r in rows})} collections.")
        print("  A missing field does not compare: `$lt`, `$gt`, `$ne: null` and an\n"
              "  equality on the default all fail to match it, and a sort treats it as\n"
              "  absent. Check each one for a reader before deciding it is harmless.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"cutover": CUTOVER.isoformat(),
                                         "post_cutover_only": not args.all,
                                         "rows": rows}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
