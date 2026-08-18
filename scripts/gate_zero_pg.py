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
                  `from app.db.connection import ...` / `from app.db import
                  connection` / `import app.db.connection`
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
    python scripts/gate_zero_pg.py --root <a checkout of master>
must report a large nonzero count. A gate that has never been seen to fail is
not evidence. `--self-test` runs that control automatically against the
repo's own git history.

Usage:
    python scripts/gate_zero_pg.py
    python scripts/gate_zero_pg.py --json reports/gate_zero_pg.json
    python scripts/gate_zero_pg.py --show get_db_call --limit 40
    python scripts/gate_zero_pg.py --self-test
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

# Files whose whole purpose is to build or retire the Postgres schema. They are
# deleted at teardown rather than converted; counting them every run would
# make the number move for a reason that is not conversion progress. They are
# still reported, under `schema_files`, so the deletion cannot be forgotten.
SCHEMA_FILES = {
    "app/db/migrations.py",
    "app/db/init_db.py",
    "app/db/schema_pg.sql",
    "app/utils/db_migrations.py",
}

# Files that must KEEP talking to Postgres, and why. These are not unfinished
# work and counting them would make the gate's target unreachable — but they
# are printed on every run, because an exemption nobody re-reads is how a
# permanent exception gets granted to something temporary.
#
# `table_spec` reads `information_schema.columns` to build the column list and
# type mappers the PG->Mongo backfill uses. Its only callers are
# pg_to_mongo_backfill.py, migrate_all.py and check_generated_specs.py.
# Converting it to Mongo would mean asking the destination store to describe
# the source schema, which it cannot do — and would break the migration
# itself. It moves to scripts/migration/ at teardown, with psycopg, so the
# application image can drop the driver while the parity tooling keeps working
# against the frozen Postgres backup.
MIGRATION_TOOLING = {
    "app/db/table_spec.py": "reads information_schema for the backfill mappers; "
                            "moves to scripts/migration/ at teardown",
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
            elif alias.name in ("app.db.connection",):
                self._add("connection_import", node, f"import {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".")[0]
        names = ", ".join(a.name for a in node.names)
        if root in DRIVER_ROOTS:
            self._add("driver_import", node, f"from {module} import {names}")
        elif module == "app.db.connection":
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


def self_test() -> int:
    """Negative control: the gate must report nonzero against master.

    A gate is only evidence if it has been seen to fail. This checks out
    master into a temp dir and asserts the same scan finds couplings there.
    """
    print("Gate 1 self-test — the gate must FAIL on master (negative control)")
    print()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                ["git", "archive", "--format=tar", "master"],
                cwd=REPO,
                check=True,
                stdout=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            print(f"  could not read master: {exc}")
            return 2
        tar = subprocess.run(
            ["git", "archive", "--format=tar", "master"],
            cwd=REPO,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=tar, check=True)
        control = scan(Path(tmp))

    live = scan(REPO)
    print(f"  master (control): {control['total']:5} couplings in {control['file_count']} files")
    print(f"  HEAD    (live)  : {live['total']:5} couplings in {live['file_count']} files")
    print()
    if control["total"] == 0:
        print("  FAIL: the control reports zero. The gate cannot detect couplings;")
        print("        a zero from it would mean nothing.")
        return 2
    print("  PASS: the gate reports nonzero on master, so a zero on HEAD is meaningful.")
    if live["total"] < control["total"]:
        print(f"  progress: {control['total'] - live['total']} couplings removed on this branch.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(REPO), help="repo root to scan")
    ap.add_argument("--json", help="write the full finding list here")
    ap.add_argument("--show", choices=KINDS, help="print individual sites of this kind")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--self-test", action="store_true", help="run the negative control against master")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    result = scan(Path(args.root).resolve())
    render(result, args.show, args.limit)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print()
        print(f"wrote {out}")

    if result["errors"]:
        return 2
    return 0 if result["total"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
