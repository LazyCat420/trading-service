#!/usr/bin/env python3
"""Prove that every table's migration key actually identifies one row.

The backfill upserts on the key `app/db/table_spec.key_fields_for()` returns,
which comes from the GENERATED `migration_ledger.json`. A generated catalog is
a cache, and this one has no invalidation: if its recorded key is narrower than
the table's real identity, the backfill silently upserts many Postgres rows onto
ONE Mongo document and the loss looks like a count mismatch at the very end —
or like nothing at all, if nobody compares.

That is not hypothetical. `cycle_summaries` is keyed `cycle_id` in the ledger,
which recorded `UNIQUE (cycle_id)`. No such constraint exists: the live schema
has `PRIMARY KEY (ticker, cycle_id)` and a plain, non-unique index on
`cycle_id`. Backfilling it collapsed 2,499 rows into 262 documents — an 89.5%
loss that the ledger asserted was impossible.

Two checks per table, both against the LIVE schema, never the ledger:

  1. AGREEMENT   the ledger's key matches the table's primary key.
  2. UNIQUENESS  count(*) == count(DISTINCT key) — the decisive one, because it
                 tests the data rather than the declaration. A key can disagree
                 with the PK and still be unique; a key that fails this WILL
                 lose rows.

Exit codes: 0 all keys sound · 1 at least one key loses rows · 2 a table could
not be checked (treated as failure — an unchecked table is not a passing one).

Usage:
    python scripts/check_natural_keys.py
    python scripts/check_natural_keys.py --max-rows 2000000   # skip huge scans
    python scripts/check_natural_keys.py --table cycle_summaries
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from scripts.quality_census import pg_url  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "app" / "db" / "migration_ledger.json"


def ledger_rows() -> dict[str, dict]:
    data = json.loads(LEDGER.read_text())
    rows = data["tables"] if isinstance(data, dict) else data
    return {r["table"]: r for r in rows}


def declared_key(row: dict) -> list[str]:
    """The key table_spec.key_fields_for() will hand the backfill."""
    raw = (row.get("natural_key") or row.get("key_field") or "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def primary_keys(cur) -> dict[str, list[str]]:
    cur.execute(
        """
        SELECT c.relname, array_agg(a.attname ORDER BY k.ord)
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
        WHERE con.contype = 'p'
        GROUP BY c.relname
        """
    )
    return {t: cols for t, cols in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", help="check one table only")
    ap.add_argument("--max-rows", type=int, default=0,
                    help="skip the uniqueness scan above this row count (0 = no limit)")
    args = ap.parse_args()

    led = ledger_rows()
    problems, unchecked, checked = [], [], 0

    with psycopg.connect(pg_url(), connect_timeout=30) as conn:
        cur = conn.cursor()
        pks = primary_keys(cur)
        cur.execute(
            """SELECT c.relname FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'public' AND c.relkind = 'r'"""
        )
        live = {r[0] for r in cur.fetchall()}

        targets = [args.table] if args.table else sorted(live)
        print(f"{'TABLE':<34} {'ROWS':>10} {'DISTINCT':>10}  KEY")
        for t in targets:
            row = led.get(t)
            if t not in live or not row or row.get("disposition") != "migrate":
                continue
            keys = declared_key(row)
            if not keys:
                unchecked.append((t, "no natural_key or key_field in ledger"))
                continue

            cur.execute(f'SELECT count(*) FROM "{t}"')
            n = cur.fetchone()[0]
            if n == 0:
                continue
            if args.max_rows and n > args.max_rows:
                unchecked.append((t, f"skipped: {n:,} rows > --max-rows"))
                continue

            cols = ", ".join(f'"{k}"' for k in keys)
            try:
                cur.execute(f'SELECT count(*) FROM (SELECT {cols} FROM "{t}" GROUP BY {cols}) d')
                distinct = cur.fetchone()[0]
            except Exception as exc:
                conn.rollback()
                unchecked.append((t, f"{type(exc).__name__}: {exc}"[:120]))
                continue

            checked += 1
            pk = pks.get(t, [])
            agrees = set(keys) == set(pk) if pk else None
            unique = distinct == n
            flag = ""
            if not unique:
                flag = f"   <<< LOSES {n - distinct:,} ROWS ({100*(n-distinct)/n:.1f}%)"
                problems.append({
                    "table": t, "rows": n, "distinct": distinct,
                    "lost": n - distinct, "key": keys, "primary_key": pk,
                })
            elif agrees is False:
                flag = f"   (differs from PK {pk}, but is unique)"
            print(f"{t:<34} {n:>10,} {distinct:>10,}  {keys}{flag}")

    print(f"\nchecked {checked} tables")
    if unchecked:
        print(f"\n{len(unchecked)} NOT CHECKED — an unchecked table is not a passing one:")
        for t, why in unchecked:
            print(f"  {t:<34} {why}")
    if problems:
        print(f"\n{len(problems)} TABLE(S) WOULD LOSE ROWS ON BACKFILL:")
        for p in problems:
            print(f"  {p['table']}: key {p['key']} keeps {p['distinct']:,} of "
                  f"{p['rows']:,} rows; real PK is {p['primary_key']}")
        return 1
    return 2 if unchecked and not args.max_rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
