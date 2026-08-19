#!/usr/bin/env python3
"""Remove `from scripts.migration.pg_connection import ...` lines whose names are never used.

The conversion deleted call sites and left the imports behind. A dead import is
not cosmetic: it is a live edge into `connection.py`, and `connection.py` is
what the teardown has to delete, so Gate 1 counts it.

HOW THE FIRST VERSION OF THIS GOT IT WRONG
------------------------------------------
It counted `ast.Name` nodes for each imported name and called the import dead
when the count was <= 1, reasoning that the import itself contributed one. It
does not: `ImportFrom` holds `alias` nodes, not `Name` nodes. So a file with
exactly one real `get_db()` call counted 1, was judged dead, and lost its
import -- 34 files left calling a name they no longer imported.

Worse, the check I ran said they were fine. Importing a module does not execute
its function bodies, so a missing name inside `def collect()` raises NameError
at CALL time, not at import time. `python -c "import app.collectors.x"` reports
success for a file that cannot run. It took an IDE diagnostic on an unrelated
edit to surface it.

So this version:
  * counts real uses by walking for `ast.Name`/`ast.Attribute` nodes that are
    NOT the import alias, with no arithmetic fudge for the import itself;
  * verifies the result with `compile()` plus a symbol check that looks inside
    function bodies -- the check the first attempt lacked;
  * refuses to touch a file whose imported name appears anywhere in the AST.

Usage:
    python scripts/drop_dead_pg_imports.py           # report
    python scripts/drop_dead_pg_imports.py --apply
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app"

# Retired by deletion at teardown, not edited now.
SCHEMA_FILES = {
    "scripts/migration/pg_init_db.py",
    "scripts/migration/pg_migrations.py",
    "app/db/connection.py",
    "scripts/migration/pg_db_migrations.py",
}


def _used_names(tree: ast.AST, aliases: set[ast.alias]) -> set[str]:
    """Every name the module actually references, excluding import aliases."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
            if isinstance(node.value, ast.Name):
                used.add(node.value.id)
    return used


def _free_names(source: str) -> set[str]:
    """Names referenced anywhere, including inside function bodies.

    This is the check the first attempt did not have. `import module` does not
    run function bodies, so it cannot see a NameError waiting inside one.
    """
    tree = ast.parse(source)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def process(path: Path, apply: bool) -> tuple[int, list[str]]:
    rel = path.relative_to(REPO).as_posix()
    if rel in SCHEMA_FILES:
        return 0, []
    source = path.read_text(encoding="utf-8")
    if "scripts.migration.pg_connection" not in source:
        return 0, []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, [f"{rel}: does not parse, skipped"]

    aliases = {a for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and n.module == "scripts.migration.pg_connection" for a in n.names}
    used = _used_names(tree, aliases)

    lines = source.splitlines(keepends=True)
    notes: list[str] = []
    drop_lines: list[int] = []
    removed = 0

    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.module == "scripts.migration.pg_connection"):
            continue
        keep = [a for a in node.names if (a.asname or a.name) in used]
        if keep:
            if len(keep) < len(node.names):
                notes.append(
                    f"{rel}:{node.lineno}: keeping {[a.name for a in keep]}, "
                    f"dropping {[a.name for a in node.names if a not in keep]}"
                )
                names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in keep)
                indent = " " * (len(lines[node.lineno - 1]) - len(lines[node.lineno - 1].lstrip()))
                lines[node.lineno - 1] = f"{indent}from scripts.migration.pg_connection import {names}\n"
                for ln in range(node.lineno, node.end_lineno or node.lineno):
                    drop_lines.append(ln)
                removed += len(node.names) - len(keep)
            continue
        for ln in range(node.lineno - 1, node.end_lineno or node.lineno):
            drop_lines.append(ln)
        removed += len(node.names)

    if not removed:
        return 0, notes

    for ln in sorted(set(drop_lines), reverse=True):
        del lines[ln]
    new_source = "".join(lines)

    # Proof 1: it still compiles.
    try:
        compile(new_source, rel, "exec")
    except SyntaxError as exc:
        return 0, [f"{rel}: rewrite broke syntax ({exc}), left alone"]

    # Proof 2: no name is now free that was bound by the import we removed.
    # This is what the first attempt was missing.
    before_free = _free_names(source)
    after_tree = ast.parse(new_source)
    still_bound = set()
    for node in ast.walk(after_tree):
        if isinstance(node, ast.ImportFrom):
            still_bound.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            still_bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            still_bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            still_bound.add(node.id)
    orphaned = ({a.asname or a.name for a in aliases} & before_free) - still_bound
    if orphaned:
        return 0, [f"{rel}: would orphan {sorted(orphaned)} — REFUSED (this is the bug that broke 34 files)"]

    if apply:
        path.write_text(new_source, encoding="utf-8")
    return removed, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    total = 0
    files = 0
    all_notes: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        removed, notes = process(path, args.apply)
        all_notes.extend(notes)
        if removed:
            total += removed
            files += 1
            print(f"  {removed:2}  {path.relative_to(REPO).as_posix()}")

    for note in all_notes:
        print(f"       {note}")
    print()
    print(f"{'removed' if args.apply else 'would remove'} {total} dead import name(s) in {files} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
