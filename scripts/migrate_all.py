#!/usr/bin/env python3
"""Sweep every migrate-scope table with data into MongoDB, smallest first.

Wraps `pg_to_mongo_backfill.backfill()` — it moves one table, this decides the
order, throttles the big ones, survives a failing table, and writes a report
that names every table it did NOT move.

Order is smallest-first so a spec problem surfaces on a 6-row table in a second
rather than four hours into `price_history`. The two large time-series tables
run last and rate-limited: the oplog is capped at 2.15 GB with a ~46 hour
window, and an unthrottled 15.7M-row load would roll it.

This seeds Mongo. It does not flip any read path — `mongo_backends.env` is
untouched, so every table keeps serving from wherever its flag says. Re-running
is safe and idempotent (upsert on the natural key), which is what makes it
correct to re-run at each table's real cutover.

Usage:
    python scripts/migrate_all.py --dry-run
    python scripts/migrate_all.py
    python scripts/migrate_all.py --only price_history --rate-limit 4000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from scripts.pg_to_mongo_backfill import backfill  # noqa: E402
from scripts.quality_census import pg_url  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"
LEDGER = REPO / "app" / "db" / "migration_ledger.json"

# Tables big enough that an unthrottled load is an operational event.
# rows -> rows/sec cap
THROTTLE = {
    "price_history": 20000,
    "technicals": 20000,
}
LARGE_ROW_THRESHOLD = 1_000_000

# Already fully on Mongo — Postgres is not the source of truth for it.
SKIP = {"embeddings"}


def ensure_key_index(table: str) -> str:
    """Index the natural key BEFORE backfilling, or the load is quadratic.

    The backfill upserts with `UpdateOne({key: ...}, upsert=True)`. Without an
    index on that key every single upsert is a COLLECTION SCAN, so a batch of
    2,000 against a 40,000-document collection examines 80 million documents.
    Measured on macro_indicators, which had only its `_id` index: ~29 rows/sec,
    getting slower as the collection grew. price_history (15.7M rows) would
    never have finished.

    The index is created on the same fields the backfill keys on — read from
    table_spec, not guessed — so it always matches the upsert filter.
    """
    from scripts.migration import pg_connection as connection, table_spec
    from app.db.collections import collection_for
    from app.db.mongo_store import get_doc_db

    with connection.get_db() as db:
        _, keys, _ = table_spec.spec_for(table, db)
    keys = [keys] if isinstance(keys, str) else list(keys)
    coll = get_doc_db()[collection_for(table)]
    existing = {tuple(i["key"].keys()) for i in coll.list_indexes()}
    if tuple(keys) in existing:
        return f"index on {keys} already present"
    # Not unique: several collections were mirrored before this ran and may
    # already hold duplicates on the key. A unique index would fail to build
    # and abort the table; deduplication is a separate, deliberate step.
    coll.create_index([(k, 1) for k in keys], name="natural_key", background=True)
    return f"created index on {keys}"


def migrate_scope() -> dict[str, str]:
    data = json.loads(LEDGER.read_text())
    rows = data["tables"] if isinstance(data, dict) else data
    return {r["table"]: r.get("disposition") for r in rows}


def plan(conn) -> list[tuple[str, int]]:
    """(table, rows) for every migrate-scope table that still holds data."""
    cur = conn.cursor()
    cur.execute(
        """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind = 'r'"""
    )
    live = {r[0] for r in cur.fetchall()}
    scope = migrate_scope()
    out = []
    for t in sorted(live):
        if scope.get(t) != "migrate" or t in SKIP:
            continue
        cur.execute(f'SELECT count(*) FROM "{t}"')
        n = cur.fetchone()[0]
        if n:
            out.append((t, n))
    out.sort(key=lambda x: x[1])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the plan only")
    ap.add_argument("--only", nargs="*", help="restrict to these tables")
    ap.add_argument("--skip-large", action="store_true",
                    help="skip tables over 1M rows (run them separately)")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--rate-limit", type=float, default=0.0,
                    help="override the per-table throttle")
    args = ap.parse_args()

    with psycopg.connect(pg_url(), connect_timeout=30) as conn:
        work = plan(conn)

    if args.only:
        want = set(args.only)
        work = [(t, n) for t, n in work if t in want]
        missing = want - {t for t, _ in work}
        if missing:
            print(f"not in scope / no rows: {', '.join(sorted(missing))}", file=sys.stderr)
    if args.skip_large:
        work = [(t, n) for t, n in work if n < LARGE_ROW_THRESHOLD]

    total_rows = sum(n for _, n in work)
    print(f"{len(work)} tables, {total_rows:,} rows")
    if args.dry_run:
        for t, n in work:
            rl = args.rate_limit or THROTTLE.get(t, 0)
            print(f"  {t:<38} {n:>12,}" + (f"   throttled to {rl:,.0f} rows/s" if rl else ""))
        return 0

    results, failures = [], []
    started = time.monotonic()
    for i, (t, n) in enumerate(work, 1):
        rl = args.rate_limit or THROTTLE.get(t, 0.0)
        print(f"\n--- [{i}/{len(work)}] {t}  ({n:,} rows)"
              + (f"  @ {rl:,.0f} rows/s" if rl else ""))
        t0 = time.monotonic()
        try:
            print(f"[{t}] {ensure_key_index(t)}")
            rc = backfill(t, batch=args.batch, rate_limit=rl)
        except Exception as exc:  # one table must not end the sweep
            print(f"[{t}] ERROR (sweep continues): {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            failures.append({"table": t, "rows": n,
                             "error": f"{type(exc).__name__}: {exc}"[:300]})
            results.append({"table": t, "pg_rows": n, "status": "error"})
            continue
        elapsed = time.monotonic() - t0
        status = "ok" if rc == 0 else "mismatch"
        if rc != 0:
            failures.append({"table": t, "rows": n, "error": "count mismatch"})
        results.append({"table": t, "pg_rows": n, "status": status,
                        "seconds": round(elapsed, 1)})

    wall = time.monotonic() - started
    ok = [r for r in results if r["status"] == "ok"]
    print(f"\n=== {len(ok)}/{len(work)} tables OK in {wall/60:.1f} min ===")
    if failures:
        # Name them. A sweep that reports only its successes is
        # indistinguishable from one that never reached the rest.
        print(f"\n{len(failures)} table(s) did NOT migrate cleanly:", file=sys.stderr)
        for f in failures:
            print(f"  {f['table']:<38} {f['rows']:>10,}  {f['error']}", file=sys.stderr)

    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "migrate_all_report.json").write_text(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "tables_attempted": len(work),
        "tables_ok": len(ok),
        "rows_attempted": total_rows,
        "wall_seconds": round(wall, 1),
        "results": results,
        "failures": failures,
    }, indent=2))
    print(f"\nwrote {REPORTS/'migrate_all_report.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
