#!/usr/bin/env python3
"""Delete `with get_db() as db:` blocks whose body never touches `db`.

The conversion left these behind: the body was rewritten to call `mongo_store`
but the wrapper stayed, so the process still opens a pooled PostgreSQL
connection, holds it for the duration of a Mongo write, and returns it. They
are pure cost and pure coupling, and they are the last thing standing between
several files and zero Postgres.

WHY A SCRIPT AND NOT A CODEMOD BY HAND
--------------------------------------
Removing a `with` means de-indenting its body, and doing that by eye is what
broke `app/processors/quant_processor.py` on this branch: commit 5e4e1d0
dropped a wrapper from `get_correlations()` without de-indenting, orphaning 35
lines at the old level and leaving the module unimportable. A syntax error
survived forty commits and four thousand tests.

So this tool:
  * finds the blocks with the AST, never a regex;
  * refuses any block whose body references the `db` name at all (those are
    live Postgres, not vestigial — including `db.executemany`, which a grep
    for `.execute(` silently misses);
  * refuses a block with anything on the `with` line other than the single
    `get_db()` item;
  * re-parses the rewritten module and compares the AST dump against the
    original with the `With` node spliced out, so an indentation mistake
    cannot be written to disk;
  * leaves the file untouched if any check fails.

Usage:
    python scripts/unwrap_vestigial_get_db.py            # report only
    python scripts/unwrap_vestigial_get_db.py --apply
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app"


def _is_get_db_with(node: ast.AST) -> bool:
    if not isinstance(node, ast.With) or len(node.items) != 1:
        return False
    call = node.items[0].context_expr
    if not isinstance(call, ast.Call):
        return False
    name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
    return name == "get_db" and not call.args and not call.keywords


def _bound_name(node: ast.With) -> str | None:
    var = node.items[0].optional_vars
    return var.id if isinstance(var, ast.Name) else None


def _body_uses(node: ast.With, name: str | None) -> bool:
    """True if anything in the body references the bound cursor name."""
    if name is None:
        return False
    for child in ast.walk(node):
        if child is node:
            continue
        if isinstance(child, ast.Name) and child.id == name:
            # the binding itself is not a use
            if child is node.items[0].optional_vars:
                continue
            return True
    return False


class _Splicer(ast.NodeTransformer):
    """Replace target `With` nodes with their bodies — the expected AST."""

    def __init__(self, targets: set[int]) -> None:
        self.targets = targets

    def visit_With(self, node: ast.With):
        self.generic_visit(node)
        if id(node) in self.targets or (
            _is_get_db_with(node) and not _body_uses(node, _bound_name(node))
        ):
            return node.body
        return node


def unwrap_source(source: str) -> tuple[str, int, list[str]]:
    """Return (new_source, blocks_removed, skipped_reasons)."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    skipped: list[str] = []

    targets = []
    for node in ast.walk(tree):
        if not _is_get_db_with(node):
            continue
        name = _bound_name(node)
        if _body_uses(node, name):
            skipped.append(f"line {node.lineno}: body still uses `{name}`")
            continue
        targets.append(node)

    if not targets:
        return source, 0, skipped

    # Rewrite from the bottom up so earlier line numbers stay valid.
    for node in sorted(targets, key=lambda n: n.lineno, reverse=True):
        with_idx = node.lineno - 1
        body_start = node.body[0].lineno - 1
        body_end = max(
            getattr(n, "end_lineno", n.lineno) for n in ast.walk(node) if hasattr(n, "lineno")
        )
        with_indent = len(lines[with_idx]) - len(lines[with_idx].lstrip())
        body_indent = len(lines[body_start]) - len(lines[body_start].lstrip())
        shift = body_indent - with_indent
        if shift <= 0:
            skipped.append(f"line {node.lineno}: body is not indented past the `with`")
            continue

        new_body = []
        for raw in lines[body_start:body_end]:
            if not raw.strip():
                new_body.append(raw)  # blank lines carry no indentation
            elif raw.startswith(" " * shift):
                new_body.append(raw[shift:])
            else:
                # A line indented less than the body start — a continuation of
                # something odd. Bail on this block rather than mangle it.
                new_body = None
                skipped.append(f"line {node.lineno}: ragged indentation, left alone")
                break
        if new_body is None:
            continue
        lines[with_idx:body_end] = new_body

    new_source = "".join(lines)

    # The proof: the rewritten module must parse, and must be exactly the
    # original with the `With` wrappers spliced out. This is what makes an
    # indentation bug impossible to commit.
    try:
        new_tree = ast.parse(new_source)
    except SyntaxError as exc:
        raise RuntimeError(f"rewrite produced a syntax error: {exc}") from exc

    expected = _Splicer(set()).visit(ast.parse(source))
    ast.fix_missing_locations(expected)
    if ast.dump(new_tree) != ast.dump(expected):
        raise RuntimeError("rewrite changed the program, not just the wrapper")

    return new_source, len(targets) - len([s for s in skipped if "ragged" in s]), skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("paths", nargs="*", help="files to process (default: all of app/)")
    args = ap.parse_args()

    paths = [Path(p) for p in args.paths] or sorted(APP.rglob("*.py"))
    total_blocks = 0
    changed: list[str] = []

    for path in paths:
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "get_db()" not in source:
            continue
        try:
            new_source, removed, skipped = unwrap_source(source)
        except (SyntaxError, RuntimeError) as exc:
            print(f"  SKIP {path.relative_to(REPO)}: {exc}")
            continue
        if removed:
            rel = path.relative_to(REPO).as_posix()
            print(f"  {removed:2} block(s)  {rel}")
            total_blocks += removed
            changed.append(rel)
            if args.apply:
                path.write_text(new_source, encoding="utf-8")
        for reason in skipped:
            print(f"       skipped {path.relative_to(REPO)} {reason}")

    print()
    verb = "removed" if args.apply else "would remove"
    print(f"{verb} {total_blocks} vestigial get_db block(s) in {len(changed)} file(s)")
    if not args.apply and total_blocks:
        print("re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
