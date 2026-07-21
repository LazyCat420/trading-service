#!/usr/bin/env python3
"""
Backfill a Postgres table into its MongoDB document collection, then verify.

Part of the Postgres → MongoDB consolidation (.agents/PLAN-mongodb-consolidation.md).
Reads a PG table in batches and upserts documents into the trading_bot Mongo DB
(via app.db.mongo_store), keyed on the table's natural key so re-runs are
idempotent. Read-only on Postgres.

Usage (inside the trading-service container or with its venv + PYTHONPATH):
    python scripts/pg_to_mongo_backfill.py pipeline_events
    python scripts/pg_to_mongo_backfill.py pipeline_events --verify-only
    python scripts/pg_to_mongo_backfill.py --list

Each supported table declares: the SELECT, the natural key field(s), and a
row→document mapper. Add a table by adding one entry to TABLES.
"""
import argparse
import json
import sys

from app.db import connection
from app.db import mongo_store


def _pipeline_events_doc(row, cols):
    d = dict(zip(cols, row))
    data = d.get("data_json")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    return {
        "id": d["id"],
        "cycle_id": d.get("cycle_id"),
        "timestamp": d.get("timestamp"),
        "phase": d.get("phase"),
        "step": d.get("step"),
        "detail": d.get("detail"),
        "status": d.get("status"),
        "data": data or {},
        "elapsed_ms": d.get("elapsed_ms") or 0,
    }


def _passthrough_doc(row, cols):
    """id + scalar columns straight through as a document (no JSON re-parse)."""
    return dict(zip(cols, row))


def _cycle_audit_doc(row, cols):
    d = dict(zip(cols, row))
    data = d.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    d["data"] = data or {}
    return d


# table -> (select_sql, key_field, row_mapper)
TABLES = {
    "pipeline_events": (
        "SELECT id, cycle_id, timestamp, phase, step, detail, status, data_json, elapsed_ms "
        "FROM pipeline_events",
        "id",
        _pipeline_events_doc,
    ),
    "execution_errors": (
        "SELECT id, cycle_id, phase, ticker, error_type, error_message, stack_trace, created_at "
        "FROM execution_errors",
        "id",
        _passthrough_doc,
    ),
    "cycle_audit_log": (
        "SELECT id, cycle_id, timestamp, audit_type, event_type, phase, ticker, severity, message, data "
        "FROM cycle_audit_log",
        "id",
        _cycle_audit_doc,
    ),
}


def backfill(table: str, batch: int = 2000, verify_only: bool = False) -> int:
    if table not in TABLES:
        print(f"unknown table {table!r}; known: {', '.join(TABLES)}", file=sys.stderr)
        return 2
    select_sql, key_field, mapper = TABLES[table]

    with connection.get_db() as db:
        pg_count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    mongo_before = mongo_store.count_docs(table)
    print(f"[{table}] postgres rows={pg_count}  mongo docs(before)={mongo_before}")

    if not verify_only:
        moved = 0
        # Server-side pagination by key to avoid loading the whole table.
        last_key = ""
        while True:
            with connection.get_db() as db:
                cur = db.execute(
                    f"{select_sql} WHERE {key_field} > %s ORDER BY {key_field} ASC LIMIT %s",
                    [last_key, batch],
                )
                rows = cur.fetchall()
                cols = [c[0] for c in cur.description]
            if not rows:
                break
            docs = [mapper(r, cols) for r in rows]
            # One bulk round-trip per batch (not one upsert per doc).
            mongo_store.bulk_upsert(table, docs, key_field=key_field)
            moved += len(docs)
            last_key = dict(zip(cols, rows[-1]))[key_field]
            print(f"[{table}] upserted {moved}/{pg_count}", end="\r", flush=True)
        print()

    mongo_after = mongo_store.count_docs(table)
    ok = mongo_after >= pg_count
    print(f"[{table}] VERIFY: postgres={pg_count}  mongo={mongo_after}  "
          f"{'OK' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table", nargs="?", help="table to backfill")
    ap.add_argument("--verify-only", action="store_true", help="count-compare only, no writes")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--list", action="store_true", help="list supported tables")
    args = ap.parse_args()
    if args.list or not args.table:
        print("supported tables:", ", ".join(TABLES))
        return 0
    return backfill(args.table, batch=args.batch, verify_only=args.verify_only)


if __name__ == "__main__":
    raise SystemExit(main())
