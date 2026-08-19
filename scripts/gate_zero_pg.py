#!/usr/bin/env python3
"""Gate 1 — count every remaining PostgreSQL coupling in the application.

This script IS the progress report for the Postgres->Mongo migration. The
number it prints is the only status anyone should quote: the migration has
been reported "100% complete" while 83 files still imported `get_db`, and
prose cannot survive that. A count with a file list can.

What it counts, by AST (not grep, because a docstring that *describes* the
hazard matches a grep for it and a comment that mentions psycopg does not
import it):

  driver_import   `import psycopg` / `from psycopg...` / `from pgvector...`
  connection_import
                  `from scripts.migration.pg_connection import ...` / `from app.db import
                  connection` / `import scripts.migration.pg_connection`
  get_db_call     a call to `get_db(...)`, however it was imported
  execute_call    `.execute(...)` / `.executemany(...)` on something that is
                  not a sqlite3 connection or cursor

sqlite3 is NOT Postgres. `app/scraper/core/failure_cache.py` keeps a local
sqlite cache and converting it would be a defect, not progress, so files that
import sqlite3 are excluded from `execute_call` and listed separately under
`sqlite_excluded` — visible, not silently dropped.

Exit status:
  0   zero couplings — the gate passes
  1   couplings remain (the normal state until teardown)
  2   the scan itself failed (a file could not be parsed)

Negative control (REQUIRED before trusting a zero):
    python scripts/gate_zero_pg.py --root <a checkout of a COUPLED tree>
must report a large nonzero count. A gate that has never been seen to fail is
not evidence. `--self-test` runs that control automatically, against
CONTROL_REF — a commit pinned BY SHA because it is known to contain the
defect. It used to say "master", which stopped working the day the fix
merged into master.

Usage:
    python scripts/gate_zero_pg.py
    python scripts/gate_zero_pg.py --json reports/gate_zero_pg.json
    python scripts/gate_zero_pg.py --show get_db_call --limit 40
    python scripts/gate_zero_pg.py --self-test
    python scripts/gate_zero_pg.py --self-test --control-ref <sha>
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Scanned for couplings. `scripts/` is deliberately NOT here: migration and
# parity tooling must keep talking to the frozen Postgres backup after the
# application stops, so it is held to a separate rule (see scripts/migration/).
DEFAULT_TARGETS = ("app", "cycle_main.py")

# Files whose whole purpose is to build or retire the Postgres schema. They
# moved out of `app/` at teardown (2026-08-18) rather than being converted, so
# they are already outside DEFAULT_TARGETS and contribute nothing to the count.
# They stay listed, and stay reported under `schema_files`, because the DDL
# they carry is what recreates a dropped table: anyone dropping a table has to
# delete it here in the same change, or the next `run_migrations()` call brings
# it back. That is how 40 of 57 "purged" tables returned.
SCHEMA_FILES = {
    "scripts/migration/pg_migrations.py",
    "scripts/migration/pg_init_db.py",
    "scripts/migration/schema_pg.sql",
    "scripts/migration/pg_db_migrations.py",
}

# Files that must KEEP talking to Postgres, and why. These are not unfinished
# work and counting them would make the gate's target unreachable — but they
# are printed on every run, because an exemption nobody re-reads is how a
# permanent exception gets granted to something temporary.
#
# `table_spec` emits SQL against `information_schema.columns` to build the
# column list and type mappers the PG->Mongo backfill uses. Converting it to
# Mongo would mean asking the destination store to describe the source schema,
# which it cannot do.
#
# It was slated to move to scripts/migration/ at teardown "so the application
# image can drop the driver". That premise was measured false on 2026-08-18 and
# the move was cancelled: `table_spec` imports no psycopg at all (it takes an
# open `db` handle as an argument), so it never put the driver in the image —
# `connection.py` was the repo's only psycopg importer, and it has moved. The
# module also has a live application caller now: `mongo_query._money()` reads
# `uses_decimal128()` from it, deliberately, so the read and write paths cannot
# disagree about which collections carry the dec128 policy. Moving it under
# scripts/ would make the app import from its own tooling.
#
# So it stays, and stays exempt: its `execute_call` findings are SQL aimed at
# the frozen Postgres backup, not unconverted application work.
MIGRATION_TOOLING = {
    # The soak's quiescence probe. Its whole job is to read Postgres — the
    # per-table counters in pg_stat_user_tables are the only proof the trading
    # cycle has stopped touching it that application code cannot fake. It reads
    # nothing but the statistics views, and it must keep working after the
    # application stops.
    "scripts/pg_quiescence.py": "reads pg_stat_user_tables to PROVE the cycle "
                                "has stopped reading Postgres; statistics "
                                "views only, never application tables",
    "app/db/table_spec.py": "emits information_schema SQL for the backfill "
                            "mappers, but imports no driver; stays in app/db "
                            "because mongo_query._money() reads its dec128 policy",
}

DRIVER_ROOTS = {"psycopg", "psycopg2", "pgvector", "psycopg_pool"}

KINDS = ("driver_import", "connection_import", "get_db_call", "execute_call")


@dataclass
class Finding:
    kind: str
    file: str
    line: int
    detail: str


class Scanner(ast.NodeVisitor):
    """Walk one module and record its Postgres couplings."""

    def __init__(self, relpath: str, uses_sqlite: bool) -> None:
        self.relpath = relpath
        self.uses_sqlite = uses_sqlite
        self.findings: list[Finding] = []

    def _add(self, kind: str, node: ast.AST, detail: str) -> None:
        self.findings.append(
            Finding(kind=kind, file=self.relpath, line=getattr(node, "lineno", 0), detail=detail)
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in DRIVER_ROOTS:
                self._add("driver_import", node, f"import {alias.name}")
            elif alias.name in ("scripts.migration.pg_connection",):
                self._add("connection_import", node, f"import {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".")[0]
        names = ", ".join(a.name for a in node.names)
        if root in DRIVER_ROOTS:
            self._add("driver_import", node, f"from {module} import {names}")
        elif module == "scripts.migration.pg_connection":
            self._add("connection_import", node, f"from {module} import {names}")
        elif module == "app.db" and any(a.name == "connection" for a in node.names):
            self._add("connection_import", node, f"from {module} import {names}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr

        if name == "get_db":
            self._add("get_db_call", node, "get_db(...)")
        elif name in ("execute", "executemany") and not self.uses_sqlite:
            # `.execute` also exists on sqlalchemy, on mongo's own command
            # helpers and on subprocess wrappers. Require a string-ish first
            # argument so this counts SQL, not every method called execute.
            if node.args and _looks_like_sql(node.args[0]):
                self._add("execute_call", node, f".{name}(<sql>)")
        self.generic_visit(node)


def _looks_like_sql(arg: ast.AST) -> bool:
    """True when the first argument could be a SQL string."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return True
    # f-strings and concatenations that build SQL at runtime
    if isinstance(arg, (ast.JoinedStr, ast.BinOp)):
        return True
    # a module-level SQL constant passed by name
    if isinstance(arg, ast.Name) and (
        "SQL" in arg.id.upper() or "QUERY" in arg.id.upper()
    ):
        return True
    return False


def _iter_python_files(root: Path, targets: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for target in targets:
        path = root / target
        if path.is_file() and path.suffix == ".py":
            out.append(path)
        elif path.is_dir():
            out.extend(sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts))
    return out


def scan(root: Path, targets: tuple[str, ...] = DEFAULT_TARGETS) -> dict:
    findings: list[Finding] = []
    sqlite_excluded: list[str] = []
    schema_files_present: list[str] = []
    migration_tooling_present: list[str] = []
    errors: list[str] = []

    for path in _iter_python_files(root, targets):
        rel = path.relative_to(root).as_posix()
        if rel in SCHEMA_FILES:
            schema_files_present.append(rel)
            continue
        if rel in MIGRATION_TOOLING:
            migration_tooling_present.append(rel)
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - unreadable file
            errors.append(f"{rel}: {exc}")
            continue
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            errors.append(f"{rel}: {exc}")
            continue

        uses_sqlite = "sqlite3" in source
        if uses_sqlite:
            sqlite_excluded.append(rel)
        scanner = Scanner(rel, uses_sqlite)
        scanner.visit(tree)
        findings.extend(scanner.findings)

    # schema files that still exist on disk but were skipped above
    for rel in sorted(SCHEMA_FILES):
        if (root / rel).exists() and rel not in schema_files_present:
            schema_files_present.append(rel)

    counts = Counter(f.kind for f in findings)
    return {
        "root": str(root),
        "total": len(findings),
        "counts": {k: counts.get(k, 0) for k in KINDS},
        "files": sorted({f.file for f in findings}),
        "file_count": len({f.file for f in findings}),
        "sqlite_excluded": sorted(sqlite_excluded),
        "schema_files_present": sorted(schema_files_present),
        "migration_tooling": sorted(migration_tooling_present),
        "errors": errors,
        "findings": [asdict(f) for f in findings],
    }


def render(result: dict, show: str | None, limit: int) -> None:
    print(f"Gate 1 — PostgreSQL couplings under {result['root']}")
    print()
    for kind in KINDS:
        print(f"  {kind:20} {result['counts'][kind]:5}")
    print(f"  {'-' * 20} {'-' * 5}")
    print(f"  {'TOTAL':20} {result['total']:5}   in {result['file_count']} files")
    print()

    if result["schema_files_present"]:
        print("  schema files still on disk (deleted at teardown, not converted):")
        for rel in result["schema_files_present"]:
            print(f"    {rel}")
        print()
    for rel in result.get("migration_tooling", []):
        print(f"  migration tooling, keeps Postgres deliberately:")
        print(f"    {rel} — {MIGRATION_TOOLING[rel]}")
        print()
    if result["sqlite_excluded"]:
        print(f"  sqlite3 files excluded from execute_call ({len(result['sqlite_excluded'])}):")
        for rel in result["sqlite_excluded"]:
            print(f"    {rel}")
        print()
    if result["errors"]:
        print("  SCAN ERRORS (gate cannot certify a zero while these stand):")
        for err in result["errors"]:
            print(f"    {err}")
        print()

    if show:
        sites = [f for f in result["findings"] if f["kind"] == show]
        print(f"  {show} sites ({len(sites)}, showing {min(limit, len(sites))}):")
        for f in sites[:limit]:
            print(f"    {f['file']}:{f['line']}  {f['detail']}")
        print()

    if result["total"] == 0 and not result["errors"]:
        print("  RESULT: zero couplings.")
    else:
        top = Counter(f["file"] for f in result["findings"]).most_common(12)
        print("  top files:")
        for rel, n in top:
            print(f"    {n:5}  {rel}")


# The last commit on master that still had Postgres couplings, used as the
# negative control. This was `master` until 2026-08-19, when quality-purge
# merged and master became the converted tree — at which point the control
# reported 0, the self-test failed, and the failure said "the gate is broken"
# when what had actually happened is that the gate had won. A control has to
# name a commit that is KNOWN to contain the defect; "whatever master is now"
# stops being that the moment the fix lands.
CONTROL_REF = "a2540d1"           # pre-merge master; scan finds 1,861 couplings
CONTROL_EXPECTED = 1861


def self_test(control_ref: str = CONTROL_REF) -> int:
    """Negative control: the gate must report nonzero against a tree that is
    KNOWN to be coupled to Postgres.

    A gate is only evidence if it has been seen to fail. This checks out
    `control_ref` into a temp dir and asserts the same scan finds couplings
    there.
    """
    print(f"Gate 1 self-test — the gate must FAIL on {control_ref} (negative control)")
    print()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            tar = subprocess.run(
                ["git", "archive", "--format=tar", control_ref],
                cwd=REPO,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
        except subprocess.CalledProcessError as exc:
            print(f"  could not read {control_ref}: {exc}")
            print("  (a shallow clone will not have it — fetch the full history)")
            return 2
        subprocess.run(["tar", "-x", "-C", tmp], input=tar, check=True)
        control = scan(Path(tmp))

    live = scan(REPO)
    print(f"  {control_ref} (control): {control['total']:5} couplings in {control['file_count']} files")
    print(f"  HEAD          (live)  : {live['total']:5} couplings in {live['file_count']} files")
    print()
    if control["total"] == 0:
        print(f"  FAIL: the control reports zero. Either {control_ref} is not the")
        print("        coupled tree it is supposed to be, or the gate can no longer")
        print("        detect couplings — a zero from it would mean nothing either way.")
        return 2
    if control["total"] != CONTROL_EXPECTED:
        # Not fatal: the scan may have legitimately been sharpened. But an
        # unannounced change in what the control measures is worth saying out
        # loud, because it is how a control quietly drifts into agreeing with
        # everything.
        print(f"  NOTE: control found {control['total']}, expected {CONTROL_EXPECTED}."
              " The scan's reach changed; update CONTROL_EXPECTED deliberately.")
    print(f"  PASS: the gate reports nonzero on {control_ref}, so a zero on HEAD is meaningful.")
    if live["total"] < control["total"]:
        print(f"  progress: {control['total'] - live['total']} couplings removed since the control.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(REPO), help="repo root to scan")
    ap.add_argument("--json", help="write the full finding list here")
    ap.add_argument("--show", choices=KINDS, help="print individual sites of this kind")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--self-test", action="store_true",
                    help="run the negative control against CONTROL_REF")
    ap.add_argument("--control-ref", default=CONTROL_REF,
                    help=f"the known-coupled commit the control scans (default {CONTROL_REF})")
    ap.add_argument("--targets", nargs="*", default=list(DEFAULT_TARGETS),
                    help="paths to scan, relative to --root (default: app cycle_main.py)")
    ap.add_argument("--max", type=int, default=0, metavar="N",
                    help="RATCHET: fail above N findings instead of above 0. "
                         "For scripts/, where 0 is not reachable yet — the "
                         "operational tooling is converted but 97 files of "
                         "one-off reports and backfills still read the frozen "
                         "Postgres. A ratchet that only moves down is worth "
                         "more than a gate nobody can turn on.")
    args = ap.parse_args()

    if args.self_test:
        return self_test(args.control_ref)

    result = scan(Path(args.root).resolve(), targets=tuple(args.targets))
    render(result, args.show, args.limit)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print()
        print(f"wrote {out}")

    if result["errors"]:
        return 2
    if args.max:
        total = result["total"]
        print()
        if total > args.max:
            print(f"RATCHET FAILED: {total} findings > {args.max} allowed in "
                  f"{' '.join(args.targets)}")
            return 1
        if total < args.max:
            print(f"ratchet can tighten: {total} findings, allowance {args.max} "
                  "— lower --max in the caller")
        else:
            print(f"ratchet held at {total} findings in {' '.join(args.targets)}")
        return 0
    return 0 if result["total"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
