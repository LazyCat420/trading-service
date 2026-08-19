#!/usr/bin/env python3
"""Rewrite Postgres call sites into Mongo calls. Writes files with --apply.

Only rewrites what it can prove safe, and leaves everything else exactly as it
was. The three shapes it handles:

    db.execute(SQL, [a, b]).fetchall()  ->  mongo_query.find_rows(...)
    db.execute(SQL, [a, b]).fetchone()  ->  mongo_query.find_row(...)
    db.execute(SQL, [a, b])             ->  mongo_store.insert_docs(...) / update / delete

REFUSALS — each of these would be a silent behaviour change, so none is
attempted:

  * Parameters that are not a list/tuple LITERAL. `db.execute(sql, params)`
    where `params` is a variable cannot be destructured into positional
    arguments without knowing its length, and guessing would misalign every
    placeholder.
  * `SELECT *` feeding a positional read. A document has no column order, so
    `r[0]` has no meaning. Those sites must name their columns first.
  * Anything `sql_to_mongo.translate()` refuses.
  * A statement whose surrounding call is not one of the three shapes above
    (e.g. `cur = db.execute(...)` used later) — the rewrite would have to
    follow the variable, which is a different and much less safe transform.

Edits are applied bottom-up by source offset so earlier replacements cannot
shift the positions of later ones, and every modified file is re-parsed before
it is written: a file that no longer compiles is never saved.

Usage:
    python scripts/codemod_pg_to_mongo.py                    # report only
    python scripts/codemod_pg_to_mongo.py --apply
    python scripts/codemod_pg_to_mongo.py --apply --file app/services/watch_desk.py
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sql_to_mongo import Unsupported, translate  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app"
SKIP_FILES = {"scripts/migration/pg_migrations.py", "scripts/migration/pg_init_db.py", "scripts/migration/pg_connection.py",
              "app/db/mongo_store.py", "app/db/mongo_query.py", "app/db/table_spec.py"}
PARAM_RE = re.compile(r"\{p(\d+)\}")


def literal_params(node: ast.AST | None, src: str) -> list[str] | None:
    """Source text of each positional parameter, or None if not a literal."""
    if node is None:
        return []
    if isinstance(node, (ast.List, ast.Tuple)):
        return [ast.get_source_segment(src, e) or "" for e in node.elts]
    return None


def find_sites(tree: ast.AST, src: str):
    """Yield (outer_node, execute_call, fetch_kind) for the three safe shapes."""
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            # chained: db.execute(...).fetchall()
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr in ("fetchall", "fetchone")
                    and isinstance(child.func.value, ast.Call)
                    and isinstance(child.func.value.func, ast.Attribute)
                    and child.func.value.func.attr == "execute"):
                yield child, child.func.value, child.func.attr
    # bare statements: db.execute(...) as an expression statement
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "execute"):
            yield node.value, node.value, "none"


def build_call(t, params: list[str], fetch: str) -> str | None:
    """Match the SHAPE the call site expects, or refuse.

    `agg_row` already returns ONE tuple, exactly what fetchone() gave, so it
    passes through unchanged there and is wrapped in a list for fetchall().
    Getting this wrong is not a crash but a type error at the caller —
    `SELECT count(*)` read positionally as `row[0]` fails on a bare int — so
    every shape is spelled out rather than defaulted.
    """
    call = t.call
    if fetch == "fetchone":
        if call.startswith("mongo_query.agg_row("):
            pass                             # already a single tuple
        elif call.startswith("mongo_query.find_rows("):
            call = call.replace("mongo_query.find_rows(", "mongo_query.find_row(", 1)
            call = re.sub(r",\s*limit=\d+\s*\)$", ")", call)
        else:
            return None
    elif fetch == "fetchall":
        if call.startswith("mongo_query.agg_row("):
            call = f"[{call}]"               # one aggregate row, as a row list
        elif call.startswith(("mongo_query.find_rows(", "mongo_query.group_rows(")):
            pass                             # already a list of tuples
        else:
            return None                      # SELECT * has no column order
    else:
        if call.startswith("mongo_query."):
            return None                      # a read whose result is discarded
    # Substitute the caller's own parameter expressions, in order.
    def sub(m):
        i = int(m.group(1))
        return params[i] if i < len(params) else m.group(0)
    out = PARAM_RE.sub(sub, call)
    return None if PARAM_RE.search(out) else out


def process(path: Path, apply: bool, stats: Counter, log: list) -> bool:
    rel = str(path.relative_to(REPO))
    if rel in SKIP_FILES:
        return False
    src = path.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False

    edits = []
    for outer, exec_call, fetch in find_sites(tree, src):
        if not exec_call.args:
            continue
        sql_node = exec_call.args[0]
        if not (isinstance(sql_node, ast.Constant) and isinstance(sql_node.value, str)):
            stats["skip: sql not a literal"] += 1
            continue
        params = literal_params(exec_call.args[1] if len(exec_call.args) > 1 else None, src)
        if params is None:
            stats["skip: params not a literal list"] += 1
            continue
        try:
            t = translate(sql_node.value)
        except Unsupported as exc:
            stats[f"skip: {exc}"[:60]] += 1
            continue
        if t.n_params != len(params):
            stats["skip: param count mismatch"] += 1
            continue
        new = build_call(t, params, fetch)
        if new is None:
            stats["skip: shape not safely rewritable"] += 1
            continue
        start, end = outer.col_offset, outer.end_col_offset
        edits.append((outer.lineno, outer.end_lineno, start, end, new))
        stats["rewritten"] += 1
        log.append({"file": rel, "line": outer.lineno, "table": t.table,
                    "verb": t.verb, "new": new[:150]})

    if not edits:
        return False

    lines = src.splitlines(keepends=True)
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))
    # Bottom-up so an earlier edit cannot move a later one's offsets.
    spans = sorted(
        ((offsets[l1 - 1] + c1, offsets[l2 - 1] + c2, new)
         for l1, l2, c1, c2, new in edits),
        key=lambda s: s[0], reverse=True)
    out = src
    for a, b, new in spans:
        out = out[:a] + new + out[b:]

    # Always run the import fixer. Gating it on "mongo_query is present and
    # there is no app.db import in the first block" skipped every file that
    # only used mongo_store, and every file that already imported something
    # from app.db — those compiled fine and raised NameError at runtime.
    out = _add_import(out)
    try:
        ast.parse(out)   # never write a file that no longer compiles
    except SyntaxError as exc:
        stats["FILE SKIPPED: rewrite broke syntax"] += 1
        log.append({"file": rel, "error": f"syntax after rewrite: {exc}"})
        return False
    if apply:
        path.write_text(out)
    return True


def _add_import(src: str) -> str:
    """Insert the imports the generated code needs.

    Both halves matter. The db modules are obvious; the datetime names are not,
    and missing them is a NameError at RUNTIME rather than at import — the
    rewritten file compiles perfectly and then dies the first time that branch
    executes. SQL `NOW()` becomes `datetime.now(timezone.utc)` and an INTERVAL
    becomes a `timedelta`, so any file that gained one needs those names.
    """
    lines = src.splitlines(keepends=True)
    stmts = []

    # Decide with the AST, never a substring. `"import mongo_store" not in src`
    # was satisfied by a FUNCTION-LOCAL `from app.db import mongo_store`, which
    # puts the name in scope only inside that function — so 33 files were left
    # calling mongo_store at module scope with no import. They compiled and
    # would have raised NameError on the first call.
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    toplevel = set()
    for n in tree.body:
        if isinstance(n, ast.ImportFrom):
            toplevel.update(a.asname or a.name for a in n.names)
        elif isinstance(n, ast.Import):
            toplevel.update((a.asname or a.name).split(".")[0] for a in n.names)
    used = {n.value.id for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id in ("mongo_store", "mongo_query")}
    needed = sorted(used - toplevel)
    if needed:
        stmts.append(f"from app.db import {', '.join(needed)}\n")

    # A file with module-style `import datetime` cannot use the bare names:
    # `datetime.now()` is not a function there, and adding
    # `from datetime import datetime` would shadow the module and break the
    # file's existing `datetime.date(...)` calls. Qualify the generated code
    # instead of changing the file's import style.
    # TOP-LEVEL imports only (no leading whitespace). A function-local
    # `from datetime import datetime` does not put the name in scope anywhere
    # else in the file, but an indent-tolerant `^\s*` pattern counted it and
    # skipped the import — leaving 6 files that compiled and would NameError
    # the first time the rewritten line ran.
    module_style = (re.search(r"^import datetime\s*$", src, re.M)
                    and not re.search(r"^from datetime import", src, re.M))
    if module_style:
        src = src.replace("datetime.now(timezone.utc)",
                          "datetime.datetime.now(datetime.timezone.utc)")
        src = re.sub(r"(?<!datetime\.)\btimedelta\(", "datetime.timedelta(", src)
        return _insert(src, stmts)

    dt_needed = [
        name for name in ("datetime", "timezone", "timedelta")
        if re.search(rf"\b{name}\b\s*[.(]", src)
        and not re.search(rf"^from datetime import[^\n]*\b{name}\b", src, re.M)
    ]
    if dt_needed:
        stmts.append(f"from datetime import {', '.join(sorted(dt_needed))}\n")

    return _insert(src, stmts)


def _insert(src: str, stmts: list[str]) -> str:
    """Place `stmts` after the file's last top-level import.

    The insertion point comes from the AST, not from scanning for lines that
    start with `import`/`from`. A parenthesized import spans several lines:

        from app.autoresearch.scorecard import (
            VERDICT_CONTAMINATED, ...,
        )

    and only its FIRST line starts with `from`, so the line scan put the new
    import between that line and the names it opens -- inside the parentheses.
    The result did not parse. The codemod's own guard caught it and declined to
    write, but it still counted the sites as rewritten, so the run reported
    "APPLIED - 9 call sites rewritten" while changing nothing on disk.

    `node.end_lineno` knows where a statement actually ends.
    """
    if not stmts:
        return src
    lines = src.splitlines(keepends=True)

    last = 0
    try:
        tree = ast.parse(src)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last = max(last, (node.end_lineno or node.lineno))
            elif last:
                # first non-import statement after the import block
                break
    else:  # pragma: no cover - unparseable input
        for i, ln in enumerate(lines[:80]):
            if ln.startswith(("import ", "from ")):
                last = i + 1

    for s in reversed(stmts):
        lines.insert(last, s)
    return "".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--file", help="one file only (repo-relative)")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    files = [REPO / args.file] if args.file else sorted(APP.rglob("*.py"))
    stats, log, touched = Counter(), [], 0
    for p in files:
        if process(p, args.apply, stats, log):
            touched += 1

    # Report what reached disk, not what was attempted. The headline used to
    # read "APPLIED — 9 call sites rewritten across 0 files": every rewrite had
    # been discarded by the syntax guard, and the only signal was a skip line
    # buried in the stats table. A summary that says APPLIED when nothing was
    # applied is the failure mode this migration keeps repeating.
    broken = stats["FILE SKIPPED: rewrite broke syntax"]
    print(("APPLIED" if args.apply else "DRY RUN")
          + f" — {stats['rewritten']} call sites rewritten across {touched} files\n")
    if broken:
        print(f"  !! {broken} file(s) DISCARDED: the rewrite did not parse, "
              f"so nothing was written for them.")
        print("     Their call sites are counted in `rewritten` above but did "
              "NOT reach disk.\n")
    for k, v in stats.most_common(14):
        print(f"  {v:>5}  {k}")
    by_table = Counter(e.get("table") for e in log if "table" in e)
    print("\ntop tables converted:")
    for t, n in by_table.most_common(10):
        print(f"  {n:>4}  {t}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"stats": dict(stats), "sites": log}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
