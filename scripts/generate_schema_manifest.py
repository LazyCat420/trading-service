#!/usr/bin/env python3
"""Record what a complete trading database looks like, so drift is detectable.

    python3 scripts/generate_schema_manifest.py --write        # from production
    python3 scripts/generate_schema_manifest.py --test-db      # from :5433
    python3 scripts/generate_schema_manifest.py --diff --test-db

`information_schema` is not enough on its own. It carries tables and columns,
and carries NOTHING about partial indexes, check constraints, triggers, enums,
sequences or extensions — all of which this schema uses. A manifest built from
`information_schema` alone would pronounce a database complete while every
index over `news_articles(published_at)` was missing, which is exactly the kind
of "the tables are all there" report that made the 161-vs-214 gap survive. So
this reads `pg_catalog` as well.

Output is sorted at every level and carries `manifest_format_version`, so a
diff between two runs is a diff of the schema and not of dictionary ordering.

WHICH DATABASE THE STORED MANIFEST COMES FROM
---------------------------------------------
`app/db/schema_manifest.json` is generated from a database built by
`scripts/init_test_db.py` — that is, from WHAT THIS REPOSITORY CREATES. It is
deliberately not a snapshot of production: production also hosts tables
belonging to other services on this box (`glass_*`, `strain_aliases`,
`cognition_*`, four `*_backup_2026*` snapshots), and a manifest demanding those
would be an oracle that can never read green, which is barely better than one
that always does.

Diffing production against it is still worth doing and is a different
question — it shows where the file has fallen behind the live schema. As of
2026-08-10 that diff is 20 columns, 2 indexes and 1 constraint; see
trading-client documentation chapter 42.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

MANIFEST_FORMAT_VERSION = "1.0"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "app" / "db" / "schema_manifest.json"

# System schemas are never part of this application's shape.
_SCHEMA_FILTER = "table_schema = 'public'"


def _rows(conn, sql: str, params: tuple = ()) -> list[tuple]:
    return list(conn.execute(sql, params).fetchall())


def collect(dsn: str) -> dict:
    import psycopg

    with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
        columns: dict[str, list[dict]] = {}
        for table, column, dtype, nullable, default in _rows(conn, f"""
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE {_SCHEMA_FILTER}
            ORDER BY table_name, column_name
        """):
            columns.setdefault(table, []).append({
                "name": column,
                "type": dtype,
                "nullable": nullable == "YES",
                # The default is recorded but deliberately NOT normalized:
                # `nextval('x_id_seq')` and a literal are different facts.
                "default": default,
            })

        tables = sorted(t for (t,) in _rows(conn, f"""
            SELECT table_name FROM information_schema.tables
            WHERE {_SCHEMA_FILTER} AND table_type = 'BASE TABLE'
        """))

        indexes: dict[str, list[str]] = {}
        for table, indexdef in _rows(conn, """
            SELECT tablename, indexdef FROM pg_indexes
            WHERE schemaname = 'public' ORDER BY tablename, indexname
        """):
            indexes.setdefault(table, []).append(indexdef)

        constraints: dict[str, list[dict]] = {}
        for table, name, kind, definition in _rows(conn, """
            SELECT rel.relname, con.conname, con.contype,
                   pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = 'public'
            ORDER BY rel.relname, con.conname
        """):
            constraints.setdefault(table, []).append({
                "name": name, "type": kind, "definition": definition,
            })

        triggers: dict[str, list[str]] = {}
        for table, definition in _rows(conn, """
            SELECT rel.relname, pg_get_triggerdef(tg.oid)
            FROM pg_trigger tg
            JOIN pg_class rel ON rel.oid = tg.tgrelid
            JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = 'public' AND NOT tg.tgisinternal
            ORDER BY rel.relname, tg.tgname
        """):
            triggers.setdefault(table, []).append(definition)

        enums: dict[str, list[str]] = {}
        for name, label in _rows(conn, """
            SELECT t.typname, e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            JOIN pg_namespace ns ON ns.oid = t.typnamespace
            WHERE ns.nspname = 'public'
            ORDER BY t.typname, e.enumsortorder
        """):
            enums.setdefault(name, []).append(label)

        sequences = sorted(s for (s,) in _rows(conn, """
            SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'
        """))

        extensions = sorted(e for (e,) in _rows(conn, """
            SELECT extname FROM pg_extension
        """))

    return {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "table_count": len(tables),
        "tables": tables,
        "columns": {t: columns.get(t, []) for t in tables},
        "indexes": {t: sorted(indexes.get(t, [])) for t in tables},
        "constraints": {t: constraints.get(t, []) for t in tables},
        "triggers": {t: sorted(triggers.get(t, [])) for t in tables if t in triggers},
        "enums": enums,
        "sequences": sequences,
        "extensions": extensions,
    }


def diff(expected: dict, actual: dict) -> list[str]:
    """What `actual` is missing relative to `expected`. Extras are not failures.

    A test database with an extra table is a leftover; a test database with a
    missing index is a database that will pass a test the real one fails.
    """
    problems: list[str] = []
    exp_tables, act_tables = set(expected["tables"]), set(actual["tables"])
    for t in sorted(exp_tables - act_tables):
        problems.append(f"missing table: {t}")

    for t in sorted(exp_tables & act_tables):
        exp_cols = {c["name"] for c in expected["columns"].get(t, [])}
        act_cols = {c["name"] for c in actual["columns"].get(t, [])}
        for c in sorted(exp_cols - act_cols):
            problems.append(f"missing column: {t}.{c}")

        exp_idx = set(expected["indexes"].get(t, []))
        act_idx = set(actual["indexes"].get(t, []))
        for i in sorted(exp_idx - act_idx):
            problems.append(f"missing index: {i}")

        exp_con = {c["definition"] for c in expected["constraints"].get(t, [])}
        act_con = {c["definition"] for c in actual["constraints"].get(t, [])}
        for c in sorted(exp_con - act_con):
            problems.append(f"missing constraint on {t}: {c}")

    for name, labels in sorted(expected.get("enums", {}).items()):
        if name not in actual.get("enums", {}):
            problems.append(f"missing enum: {name}")
        elif set(labels) - set(actual["enums"][name]):
            missing = sorted(set(labels) - set(actual["enums"][name]))
            problems.append(f"enum {name} missing labels: {missing}")

    for e in sorted(set(expected.get("extensions", [])) - set(actual.get("extensions", []))):
        problems.append(f"missing extension: {e}")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--test-db", action="store_true", help="read TEST_DATABASE_URL")
    ap.add_argument("--write", action="store_true", help="write the manifest to disk")
    ap.add_argument("--diff", action="store_true",
                    help="compare the live database against the stored manifest")
    ap.add_argument("--path", default=str(DEFAULT_PATH))
    args = ap.parse_args()

    from app.config import settings
    dsn = settings.TEST_DATABASE_URL if args.test_db else settings.DATABASE_URL

    live = collect(dsn)

    if args.diff:
        stored = json.loads(Path(args.path).read_text())
        if stored.get("manifest_format_version") != MANIFEST_FORMAT_VERSION:
            print(f"manifest format {stored.get('manifest_format_version')} != "
                  f"{MANIFEST_FORMAT_VERSION} — regenerate it", file=sys.stderr)
            return 2
        problems = diff(stored, live)
        print(f"stored: {stored['table_count']} tables    live: {live['table_count']} tables")
        for p in problems[:100]:
            print(f"  {p}")
        if len(problems) > 100:
            print(f"  … and {len(problems) - 100} more")
        return 1 if problems else 0

    if args.write:
        Path(args.path).write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.path} — {live['table_count']} tables")
        return 0

    json.dump(live, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
