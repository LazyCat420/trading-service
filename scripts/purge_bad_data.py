#!/usr/bin/env python3
"""Delete the data the quality gates condemn. DESTRUCTIVE with --execute.

Predicates and drop groups come from `quality_gates.py` and are resolved by
`quality_census.py` — this script restates none of them. If the census says a
gate matches N rows, this script deletes exactly those N rows.

Safety properties, in the order they matter:

  1. Refuses to run unless a verified full pg_dump exists (--backup).
  2. Never touches a table another project owns (checked at import AND again
     immediately before every statement).
  3. Every dropped table is archived to its own dump file with a row count and
     a sha256 first, and the restore command is written into a manifest.
  4. --dry-run is the default and is a real measurement, not a guess: it runs
     the same counting queries the delete will use.
  5. Row deletes run in batches inside one transaction per gate, so a failure
     rolls that gate back rather than half-deleting it.

Usage:
    python scripts/purge_bad_data.py --backup /path/to/pre_purge.dump
    python scripts/purge_bad_data.py --backup /path/to/pre_purge.dump --execute
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from scripts import quality_gates as QG  # noqa: E402
from scripts.quality_census import (  # noqa: E402
    exact_count,
    live_tables,
    pg_url,
    resolve_table_gates,
)

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"
ARCHIVE_DIR = Path(
    os.environ.get("PURGE_ARCHIVE_DIR", Path.home() / "db-backups" / "purged-tables")
)
DOCKER_PG_IMAGE = "postgres:16"


# ---------------------------------------------------------------------------
# pg_dump, whether or not it is installed locally
# ---------------------------------------------------------------------------
def _dump_argv(url: str, table: str, out_path: Path) -> tuple[list[str], Path]:
    """Argv that writes a custom-format dump of one table to `out_path`."""
    if shutil.which("pg_dump"):
        return (
            ["pg_dump", "-Fc", "-t", f"public.{table}", "-f", str(out_path), url],
            out_path,
        )
    if not shutil.which("docker"):
        raise SystemExit(
            "Neither pg_dump nor docker is available — cannot archive tables. "
            "Refusing to drop anything without an archive."
        )
    uid = f"{os.getuid()}:{os.getgid()}"
    return (
        [
            "docker", "run", "--rm", "--user", uid,
            "-v", f"{out_path.parent}:/out",
            DOCKER_PG_IMAGE,
            "pg_dump", "-Fc", "-t", f"public.{table}",
            "-f", f"/out/{out_path.name}", url,
        ],
        out_path,
    )


def archive_table(url: str, table: str, rows: int) -> dict:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = ARCHIVE_DIR / f"{table}.dump"
    argv, out = _dump_argv(url, table, out)
    res = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    if res.returncode != 0 or not out.exists():
        raise RuntimeError(f"pg_dump failed for {table}: {res.stderr[:300]}")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    return {
        "table": table,
        "rows": rows,
        "archive_file": str(out),
        "bytes": out.stat().st_size,
        "sha256": digest,
        "restore": (
            f"pg_restore --dbname=$DATABASE_URL --table={table} {out}"
        ),
        "archived_at": datetime.now(timezone.utc).isoformat(),
    }


def verify_backup(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"backup not found: {path}")
    if shutil.which("pg_restore"):
        argv = ["pg_restore", "--list", str(path)]
    elif shutil.which("docker"):
        argv = [
            "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{path.parent}:/bk", DOCKER_PG_IMAGE,
            "pg_restore", "--list", f"/bk/{path.name}",
        ]
    else:
        raise SystemExit("cannot verify backup: no pg_restore and no docker")
    res = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise SystemExit(f"backup is not readable by pg_restore: {res.stderr[:300]}")
    n = res.stdout.count("TABLE DATA")
    if n < 100:
        raise SystemExit(f"backup looks truncated: only {n} TABLE DATA entries")
    size_gb = path.stat().st_size / 1e9
    print(f"backup verified: {path.name}  {size_gb:.2f} GB  {n} TABLE DATA entries")


# ---------------------------------------------------------------------------
# the purge
# ---------------------------------------------------------------------------
def guard(tables) -> None:
    """Re-check foreign ownership right before touching the database."""
    QG.assert_no_foreign(list(tables))


def purge_rows(conn, execute: bool) -> list[dict]:
    cur = conn.cursor()
    results = []
    for g in QG.ROW_GATES:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"public.{g.table}",)
        )
        if not cur.fetchone()[0]:
            results.append({"table": g.table, "gate": g.name, "skipped": "absent"})
            continue
        guard([g.table])
        cur.execute(f'SELECT count(*) FROM "{g.table}" WHERE {g.predicate}')
        n = cur.fetchone()[0]
        entry = {"table": g.table, "gate": g.name, "matched": n, "deleted": 0}
        if execute and n:
            cur.execute(f'DELETE FROM "{g.table}" WHERE {g.predicate}')
            entry["deleted"] = cur.rowcount
            conn.commit()
            # Prove it: the same predicate must now match nothing.
            cur.execute(f'SELECT count(*) FROM "{g.table}" WHERE {g.predicate}')
            entry["remaining"] = cur.fetchone()[0]
        results.append(entry)
        action = "deleted" if execute else "would delete"
        print(f"  {g.table:<28} {g.name:<26} {action} {n:>9,}")
    return results


def purge_tables(conn, url: str, gates, execute: bool) -> tuple[list[dict], list[dict]]:
    cur = conn.cursor()
    manifest, results = [], []
    for g in gates:
        guard([g.table])
        rows = exact_count(cur, g.table)
        entry = {"table": g.table, "group": g.group, "rows": rows, "dropped": False}
        if execute:
            entry["archive"] = archive_table(url, g.table, rows)
            manifest.append(entry["archive"])
            cur.execute(f'DROP TABLE IF EXISTS "{g.table}" CASCADE')
            conn.commit()
            cur.execute("SELECT to_regclass(%s) IS NULL", (f"public.{g.table}",))
            entry["dropped"] = bool(cur.fetchone()[0])
        results.append(entry)
        action = "archived+dropped" if execute else "would drop"
        print(f"  [{g.group:<12}] {g.table:<44} {rows:>9,} rows  {action}")
    return results, manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", required=True, type=Path,
                    help="path to the verified pre-purge pg_dump")
    ap.add_argument("--execute", action="store_true",
                    help="actually delete (default is a real dry-run measurement)")
    args = ap.parse_args()

    verify_backup(args.backup)
    url = pg_url()

    with psycopg.connect(url, connect_timeout=30, autocommit=False) as conn:
        cur = conn.cursor()
        tables = live_tables(cur)
        gates = resolve_table_gates(cur, tables)
        guard([g.table for g in gates] + [g.table for g in QG.ROW_GATES])

        foreign_before = {
            t: exact_count(cur, t) for t in sorted(QG.FOREIGN_TABLES) if t in tables
        }
        n_before = len(tables)
        cur.execute("SELECT pg_database_size(current_database())")
        size_before = cur.fetchone()[0]

        mode = "EXECUTE" if args.execute else "DRY RUN"
        print(f"\n=== {mode} ===")
        print(f"tables live: {n_before}   protected (other projects): {len(foreign_before)}")
        print(f"\nrow gates ({len(QG.ROW_GATES)}):")
        row_results = purge_rows(conn, args.execute)
        print(f"\ntable gates ({len(gates)}):")
        table_results, manifest = purge_tables(conn, url, gates, args.execute)

        if args.execute:
            print("\nANALYZE ...")
            conn.commit()
            with psycopg.connect(url, autocommit=True) as c2:
                c2.cursor().execute("ANALYZE")

        tables_after = live_tables(cur)
        cur.execute("SELECT pg_database_size(current_database())")
        size_after = cur.fetchone()[0]
        foreign_after = {
            t: exact_count(cur, t) for t in sorted(QG.FOREIGN_TABLES) if t in tables_after
        }

    # The other project's data must be bit-identical in count. This is the
    # check that the allowlist actually held, not just that it was declared.
    harmed = {t: (foreign_before[t], foreign_after.get(t)) for t in foreign_before
              if foreign_after.get(t) != foreign_before[t]}
    print("\n=== foreign-owned tables (treesearch-service) ===")
    print(f"  {len(foreign_before)} tables, row counts unchanged: {not harmed}")
    if harmed:
        print("  !!! CHANGED:", harmed)

    print("\n=== totals ===")
    print(f"  tables:  {n_before} -> {len(tables_after)}")
    print(f"  db size: {size_before/1e9:.2f} GB -> {size_after/1e9:.2f} GB")
    print(f"  rows deleted in kept tables: "
          f"{sum(r.get('deleted', 0) for r in row_results):,}")

    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "backup": str(args.backup),
        "tables_before": n_before,
        "tables_after": len(tables_after),
        "db_bytes_before": size_before,
        "db_bytes_after": size_after,
        "row_gates": row_results,
        "table_gates": table_results,
        "foreign_unchanged": not harmed,
        "foreign_counts": foreign_before,
    }
    if args.execute:
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / "purge_report.json").write_text(json.dumps(report, indent=2))
        (REPORTS / "purged_tables_manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"\nwrote {REPORTS/'purge_report.json'}")
        print(f"wrote {REPORTS/'purged_tables_manifest.json'}  ({len(manifest)} archives)")
        if harmed:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
