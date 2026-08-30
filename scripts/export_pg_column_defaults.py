#!/usr/bin/env python3
"""Snapshot every column DEFAULT the archive used to supply, as an artifact.

Postgres filled these in on INSERT. Mongo does not, and a converted writer that
listed only the columns the SQL named silently stopped supplying them — see
`scripts/mongo_default_gaps.py`, which is what reads this file.

The snapshot exists so that instrument keeps working after the archive is
closed: an instrument that needs both stores stops the day the seam is closed,
which is exactly when you most want to know whether anything is still missing.

    python scripts/export_pg_column_defaults.py            # rewrite the artifact
    python scripts/export_pg_column_defaults.py --check    # fail if it drifted
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
ARTIFACT = REPO / "docs" / "migration" / "pg_column_defaults.json"

QUERY = """
SELECT table_name, column_name, column_default, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public' AND column_default IS NOT NULL
ORDER BY table_name, ordinal_position
"""


def collect() -> dict:
    import psycopg
    from scripts.quality_census import pg_url

    tables: dict[str, dict[str, str]] = {}
    with psycopg.connect(pg_url(), connect_timeout=30) as conn:
        for table, column, default, nullable in conn.cursor().execute(QUERY).fetchall():
            # A serial primary key is replaced by Mongo's _id, not lost.
            if column == "id" and "nextval" in (default or ""):
                continue
            tables.setdefault(table, {})[column] = default
    return {
        "note": ("Column DEFAULTs the Postgres archive supplied on INSERT. Mongo "
                 "supplies none of them; a converted writer that named only the "
                 "SQL's columns dropped them silently. Read by "
                 "scripts/mongo_default_gaps.py, which needs no Postgres."),
        "source": "information_schema.columns, schema public, column_default IS NOT NULL",
        "tables": tables,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the artifact differs from the live archive")
    args = ap.parse_args()

    fresh = collect()
    n = sum(len(v) for v in fresh["tables"].values())
    if args.check:
        if not ARTIFACT.exists():
            print(f"{ARTIFACT} does not exist")
            return 1
        stored = json.loads(ARTIFACT.read_text())
        if stored.get("tables") != fresh["tables"]:
            print("the artifact has drifted from the archive; re-run without --check")
            return 1
        print(f"artifact matches the archive: {len(fresh['tables'])} tables, {n} defaulted columns")
        return 0

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
    print(f"wrote {ARTIFACT.relative_to(REPO)}: "
          f"{len(fresh['tables'])} tables, {n} defaulted columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
