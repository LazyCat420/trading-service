#!/usr/bin/env python3
"""Inventory every SQL statement in the app and say who can convert it.

The migration's real cost is not the data — it is ~1,000 hand-written SQL
statements spread over 188 files. Before writing a codemod you have to know
which statements a codemod can actually handle, and the only honest way to know
is to PARSE them, not grep them.

Each `.execute()` / `.executemany()` call is located with the Python AST, its
first argument recovered, and the SQL parsed with sqlglot. Statements are then
classified:

  ddl          CREATE / ALTER / DROP / DO — Mongo needs none of it. Index
               declarations move to collections.py; the rest is deleted.
  mechanical   single table, no join, no aggregate, no CTE, no subquery.
               A codemod can rewrite these.
  redesign     joins, GROUP BY, CTEs, window functions, set operations,
               correlated subqueries. Mongo has no joins; $lookup is not a
               drop-in. A human decides each one.
  dynamic      the SQL is an f-string or a runtime expression, so there is no
               statement to parse at this call site. Reported separately —
               these are not "hard", they are UNKNOWN, and counting them as
               either mechanical or redesign would be inventing a number.
  unparsed     sqlglot could not parse it. Also unknown, also reported.

`dynamic` and `unparsed` are deliberately NOT folded into a percentage of
convertible work. A total that quietly absorbs everything it could not read
is the kind of number that reads as coverage and is not.

Usage:
    python scripts/sql_inventory.py
    python scripts/sql_inventory.py --json reports/sql_inventory.json
    python scripts/sql_inventory.py --show redesign --limit 30
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app"

# Files that exist to build the Postgres schema. They do not get converted;
# they get retired once nothing reads Postgres.
SCHEMA_FILES = {"app/db/migrations.py", "app/db/init_db.py"}


@dataclass
class Site:
    file: str
    line: int
    kind: str              # ddl | mechanical | redesign | dynamic | unparsed
    verb: str              # SELECT / INSERT / ...
    tables: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    sql: str = ""
    schema_file: bool = False


def extract_sql(node: ast.Call) -> tuple[str | None, str]:
    """(sql, shape) — sql is None when the call site has no literal statement."""
    if not node.args:
        return None, "no-args"
    a = node.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value, "literal"
    if isinstance(a, ast.JoinedStr):
        # An f-string: recover the literal skeleton so we can still read the
        # verb and usually the table, with placeholders where values go.
        parts = []
        for v in a.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append(" ? ")
        return "".join(parts), "f-string"
    return None, "expression"


def classify(sql: str) -> tuple[str, str, list[str], list[str]]:
    """(kind, verb, tables, features)"""
    norm = " ".join(sql.split())
    verb = (norm.split() or ["?"])[0].upper().strip("(-")
    if verb in {"CREATE", "ALTER", "DROP", "DO", "PRAGMA", "TRUNCATE", "COMMENT",
                "GRANT", "SET", "VACUUM", "ANALYZE", "REINDEX"}:
        return "ddl", verb, [], []
    try:
        tree = sqlglot.parse_one(norm, dialect="postgres")
    except Exception:
        return "unparsed", verb, [], []
    if tree is None:
        return "unparsed", verb, [], []

    tables = sorted({t.name for t in tree.find_all(exp.Table) if t.name})
    features = []
    if list(tree.find_all(exp.Join)):
        features.append("join")
    if list(tree.find_all(exp.Group)):
        features.append("group_by")
    if list(tree.find_all(exp.With)):
        features.append("cte")
    if list(tree.find_all(exp.Window)):
        features.append("window")
    if list(tree.find_all(exp.Union)) or list(tree.find_all(exp.Except)) \
            or list(tree.find_all(exp.Intersect)):
        features.append("set_op")
    if list(tree.find_all(exp.Subquery)):
        features.append("subquery")
    if any(isinstance(n, (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count))
           for n in tree.walk()):
        features.append("aggregate")
    if len(tables) > 1:
        features.append("multi_table")
    if isinstance(tree, exp.Insert) and tree.args.get("conflict"):
        features.append("upsert")

    hard = {"join", "group_by", "cte", "window", "set_op", "subquery",
            "aggregate", "multi_table"}
    kind = "redesign" if hard & set(features) else "mechanical"
    return kind, verb, tables, features


def scan() -> list[Site]:
    sites: list[Site] = []
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(REPO))
        try:
            tree = ast.parse(path.read_text())
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute)
                    and f.attr in ("execute", "executemany")):
                continue
            sql, shape = extract_sql(node)
            if sql is None:
                sites.append(Site(rel, node.lineno, "dynamic", "?",
                                  sql=f"<{shape}>", schema_file=rel in SCHEMA_FILES))
                continue
            kind, verb, tables, features = classify(sql)
            if shape == "f-string" and kind == "mechanical":
                # The skeleton parsed, but a value we blanked could carry a
                # join or a subquery. Do not claim it as mechanical.
                kind = "dynamic"
                features = features + ["f-string"]
            # WHOLE statement, not a preview. This used to store the first 220
            # characters, which is fine for a printed table and wrong for an
            # artifact: scripts/verify_translations.py translates and EXECUTES
            # `sql` from this file, so 330 of 1,274 sites — 26 of the sweep's
            # mechanical SELECTs — were judged on a mutilated statement. Five
            # died as `UndefinedColumn: column "reso" does not exist` and were
            # scored ERROR against the translator; the other 21 parsed anyway,
            # with a clause missing, and their row counts were compared as if
            # they meant something. Truncation belongs in the PRINTER (main()
            # already does it), never in the record.
            sites.append(Site(rel, node.lineno, kind, verb, tables, features,
                              " ".join(sql.split()), rel in SCHEMA_FILES))
    return sites


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, help="write the full inventory here")
    ap.add_argument("--show", help="print sites of this kind")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    sites = scan()
    app_sites = [s for s in sites if not s.schema_file]
    schema_sites = [s for s in sites if s.schema_file]

    print(f"{len(sites)} SQL call sites in app/")
    print(f"  {len(schema_sites)} in schema-building files "
          f"({', '.join(sorted(SCHEMA_FILES))}) — retired, not converted")
    print(f"  {len(app_sites)} in application code\n")

    kinds = Counter(s.kind for s in app_sites)
    convertible = kinds["mechanical"] + kinds["redesign"]
    print(f"{'KIND':<12} {'COUNT':>6}   {'SHARE OF KNOWN':>15}")
    for k in ("mechanical", "redesign", "ddl", "dynamic", "unparsed"):
        n = kinds[k]
        share = f"{100*n/convertible:.1f}%" if k in ("mechanical", "redesign") and convertible else "—"
        print(f"{k:<12} {n:>6}   {share:>15}")
    print(f"\nknown DML needing conversion: {convertible}"
          f"  ({kinds['mechanical']} codemod-able, {kinds['redesign']} by hand)")
    unknown = kinds["dynamic"] + kinds["unparsed"]
    print(f"UNKNOWN (not counted above): {unknown} "
          f"— {kinds['dynamic']} dynamic, {kinds['unparsed']} unparsed. "
          f"These need a human read before they can be classified at all.")

    print("\nredesign drivers:")
    feat = Counter(f for s in app_sites if s.kind == "redesign" for f in s.features)
    for k, v in feat.most_common():
        print(f"   {k:<14} {v:>5}")

    print("\ntop files by convertible DML:")
    per = Counter(s.file for s in app_sites if s.kind in ("mechanical", "redesign"))
    for p, n in per.most_common(12):
        m = sum(1 for s in app_sites if s.file == p and s.kind == "mechanical")
        print(f"   {n:>4} ({m} mechanical)  {p}")

    print("\ntop tables by statement count:")
    tabs = Counter(t for s in app_sites if s.kind in ("mechanical", "redesign")
                   for t in s.tables)
    for t, n in tabs.most_common(12):
        print(f"   {n:>4}  {t}")

    if args.show:
        print(f"\n--- {args.show} sites ---")
        for s in [x for x in app_sites if x.kind == args.show][: args.limit]:
            print(f"{s.file}:{s.line}  [{','.join(s.features) or s.verb}]")
            print(f"    {s.sql[:150]}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {"counts": dict(kinds), "sites": [asdict(s) for s in sites]}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
