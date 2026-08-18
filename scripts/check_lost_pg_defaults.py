#!/usr/bin/env python3
"""Find converted inserts that dropped a Postgres column DEFAULT.

THE BUG CLASS
-------------
Postgres fills a column the writer omits; MongoDB does not. So every
`INSERT INTO t (a, b)` that relied on `c TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP`
became an `insert_docs("t", [{"a":…, "b":…}])` whose document simply has no
`c` — and any reader filtering on `c` stops seeing those rows.

Nothing fails. The write succeeds, the read returns an empty set, and an empty
set is indistinguishable from "nothing happened yet". Measured on 2026-08-18:

  * `watch_events.fired_at` (PG `DEFAULT CURRENT_TIMESTAMP`) was not written,
    while `_wakes_today()` and `consume_wake_context()` both filter on it — so
    the daily wake budget counted zero forever, which is an unlimited wake
    budget, and "why you woke up" never reached the desk.
  * `v3_system_commands.status` (PG `DEFAULT 'pending'`) was omitted at six
    insert sites whose consumers all filter `status='pending'` — a research
    request or a watch-desk wake was invisible to the thing meant to run it.
  * `cycle_schedules.created_at` was omitted, so the 24h bot-creation budget
    guard could never fire.

Three found by hand in one package. This looks for the rest.

WHAT IT DOES
------------
Parses `app/db/schema_pg.sql` for columns carrying a DEFAULT, then walks every
`insert_docs` / `upsert_doc` call in `app/` by AST and reports any whose
document literal omits one of that table's defaulted columns.

It reports rather than fails by default: plenty of omissions are harmless
(a nullable flag nothing reads). `--strict` narrows to the ones that bite —
a defaulted column that some query in the tree also FILTERS on, which is the
combination that produces a silently empty read.

Usage:
    python scripts/check_lost_pg_defaults.py
    python scripts/check_lost_pg_defaults.py --strict
    python scripts/check_lost_pg_defaults.py --table watch_events
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app"
SCHEMA = APP / "db" / "schema_pg.sql"

WRITE_FUNCS = {"insert_docs", "upsert_doc", "bulk_upsert"}

# A column whose default is a per-row identity (a generated key) is not the
# hazard this hunts — those are always written explicitly anyway.
_IGNORE_COLS = {"id"}


def schema_defaults() -> dict[str, dict[str, str]]:
    """{table: {column: default_expr}} for every column declaring a DEFAULT."""
    text = SCHEMA.read_text(encoding="utf-8", errors="ignore")
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(
        r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+([A-Za-z_][\w.]*)\s*\((.*?)\n\);",
        text, re.S | re.I,
    ):
        table = m.group(1).split(".")[-1]
        cols: dict[str, str] = {}
        for line in m.group(2).splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(
                ("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CONSTRAINT", "CHECK", "--")
            ):
                continue
            dm = re.match(r"([A-Za-z_]\w*)\s+.*?\bDEFAULT\s+(.+?)(?:\s+NOT NULL)?$",
                          line, re.I)
            if dm and dm.group(1).lower() not in _IGNORE_COLS:
                cols[dm.group(1).lower()] = dm.group(2).strip()
        if cols:
            out[table] = cols
    return out


READ_FUNCS = {"find_rows", "find_row", "find_docs", "find_dicts", "count",
              "count_docs", "exists", "scalar", "agg_row", "group_rows",
              "delete_docs", "update_docs", "find_one_and_update"}


def filtered_columns() -> dict[str, set[str]]:
    """{table: {column, ...}} for columns a Mongo QUERY FILTER actually reads.

    Only the filter argument of a known read/update helper counts. An earlier
    version took every dict key passed to any call whose first argument was a
    string, which swept up the DOCUMENTS being written and the projection
    dicts, so a column was "filtered on" merely by existing. That reported
    company_registry's flags as hazards when both of its readers fetch with an
    empty filter and use `doc.get(col, default)` in Python — which is immune
    to a missing field by construction.

    Being wrong in this direction is expensive: a checker that invents
    findings trains its reader to skim, and then a real one goes past.
    """
    hits: dict[str, set[str]] = defaultdict(set)
    for path in APP.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            if getattr(node.func, "attr", None) not in READ_FUNCS:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            # The filter is the argument right after the collection name.
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Dict):
                continue
            for k in node.args[1].keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    hits[first.value].add(k.value.split(".")[0].lower())

    # Readers that have NOT been converted yet still filter in SQL, and those
    # are the ones that matter most: the write moved to Mongo before the read
    # did, which is exactly the window where the row goes missing. Scanning
    # only Mongo filters made this checker blind to the very bug that
    # motivated it — watch_events.fired_at was omitted by a converted writer
    # while its readers were still `WHERE fired_at >= NOW() - INTERVAL ...`,
    # and the first version reported no hazard.
    sql_ref = re.compile(
        r"\bFROM\s+([a-z_][\w]*)\b(.*?)(?=\bFROM\b|$)", re.I | re.S)
    for path in APP.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if " FROM " not in text.upper():
            continue
        for m in sql_ref.finditer(text):
            table, tail = m.group(1), m.group(2)
            # Columns named in a WHERE / HAVING / ON clause of this statement.
            clause = re.search(r"\b(?:WHERE|HAVING|ON)\b(.*)", tail, re.I | re.S)
            if not clause:
                continue
            for col in re.findall(r"\b([a-z_][a-z0-9_]{2,})\s*(?:=|>|<|>=|<=|!=|IS\b|IN\b)",
                                  clause.group(1)[:600], re.I):
                hits[table].add(col.lower())
    return hits


def _doc_arg(node: ast.Call) -> ast.Dict | None:
    """The document literal a write call passes, if it is a literal.

    Positional, not "the first dict big enough". `upsert_doc(coll, key, doc)`
    takes a FILTER before the document, and a size heuristic picked the filter
    whenever it happened to carry three keys — which reported fred_collector's
    macro_indicators writes as omitting `source` when both of them set it
    explicitly. A checker that invents findings gets ignored, and then it is
    not a checker.
    """
    fn = getattr(node.func, "attr", None)
    if fn == "upsert_doc":
        args = node.args[2:3]          # (collection, key, document)
    else:
        args = node.args[1:2]          # insert_docs(collection, [documents])
    for arg in args:
        if isinstance(arg, ast.Dict):
            return arg
        if isinstance(arg, ast.List) and arg.elts and isinstance(arg.elts[0], ast.Dict):
            return arg.elts[0]
    return None


def scan(only_table: str | None = None) -> list[dict]:
    defaults = schema_defaults()
    filters = filtered_columns()
    findings: list[dict] = []

    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts or path.name in ("schema_pg.sql",):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (getattr(node.func, "attr", None) not in WRITE_FUNCS) or not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            table = first.value
            if only_table and table != only_table:
                continue
            if table not in defaults:
                continue
            doc = _doc_arg(node)
            if doc is None:
                continue
            written = {k.value.lower() for k in doc.keys
                       if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            missing = {c: d for c, d in defaults[table].items() if c not in written}
            if not missing:
                continue
            read_back = {c for c in missing if c in filters.get(table, set())}
            findings.append({
                "file": path.relative_to(REPO).as_posix(),
                "line": node.lineno,
                "table": table,
                "missing": missing,
                "filtered": sorted(read_back),
            })
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="only omissions of a column something also filters on")
    ap.add_argument("--table")
    args = ap.parse_args()

    findings = scan(args.table)
    if args.strict:
        findings = [f for f in findings if f["filtered"]]

    if not findings:
        print("No converted write omits a defaulted column"
              + (" that anything filters on." if args.strict else "."))
        return 0

    print(f"{len(findings)} write site(s) omit a column Postgres would have filled:\n")
    for f in sorted(findings, key=lambda x: (not x["filtered"], x["file"])):
        mark = "  !! " if f["filtered"] else "     "
        print(f"{mark}{f['file']}:{f['line']}  {f['table']}")
        for col, default in sorted(f["missing"].items()):
            flag = "  <-- FILTERED ON" if col in f["filtered"] else ""
            print(f"         {col:24} PG DEFAULT {default}{flag}")
        print()

    hot = [f for f in findings if f["filtered"]]
    if hot:
        print(f"{len(hot)} of these omit a column some query filters on. Those reads "
              "return an empty set rather than an error.")
    return 1 if (args.strict and findings) else 0


if __name__ == "__main__":
    sys.exit(main())
