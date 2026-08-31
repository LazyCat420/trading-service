"""Every literal INSERT must supply the timestamp Postgres used to supply.

The conversion rewrote each Postgres INSERT into a Mongo `insert_docs` naming
the columns the SQL named. The SQL never named a column with a DEFAULT, because
the database filled it in. So those columns vanished from every document
written after the 2026-08-19 cutover — silently, because a missing field is not
an error in Mongo, it is simply absent.

A missing timestamp is the worst version of this. `$max`, `$gte`, `$lt` and a
`sort` all skip a document that has no value, so the effect is not a wrong
number in one place — it is a document that drops out of every time-bounded
question anyone asks. Measured before this guard existed:

  model_shadow_runs.created_at   31 of the 45 shadow runs since the cutover
  tool_playbook.created_at       19 of 21, and agent_runner sorts on it
  v3_system_commands.created_at  14, and the watch desk filters on it

`scripts/mongo_default_gaps.py` measures the same thing against live data;
this is the static half, so a NEW insert cannot reintroduce the class.

Scope, stated honestly: literal `mongo_store.insert_docs("<table>", [{...}])`
only. An `$set`/`$setOnInsert` update is not covered (an update legitimately
omits most fields), nor is a document assembled dynamically. A `**spread` of a
local dict IS resolved when the dict is a literal in the same function — that
is how `graph_sync.py` writes its timestamp, and treating it as missing would
have made this guard cry wolf on correct code.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULTS = REPO / "docs" / "migration" / "pg_column_defaults.json"
_TIMESTAMP_DEFAULTS = {"now()", "CURRENT_TIMESTAMP"}


def _timestamp_columns() -> dict[str, list[str]]:
    tables = json.loads(DEFAULTS.read_text())["tables"]
    out = {}
    for table, cols in tables.items():
        stamped = [c for c, d in cols.items()
                   if (d or "").strip() in _TIMESTAMP_DEFAULTS]
        if stamped:
            out[table] = sorted(stamped)
    return out


def _literal_keys(node: ast.Dict, scope: ast.AST | None) -> set[str] | None:
    """Keys of a dict literal, resolving `**name` against `scope`.

    Returns None when a spread cannot be resolved — the call is then not judged
    rather than judged wrongly.
    """
    keys: set[str] = set()
    for k, v in zip(node.keys, node.values):
        if k is None:                       # **something
            if not (isinstance(v, ast.Name) and scope is not None):
                return None
            found = None
            for sub in ast.walk(scope):
                if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and sub.targets[0].id == v.id
                        and isinstance(sub.value, ast.Dict)):
                    found = _literal_keys(sub.value, scope)
            if found is None:
                return None
            keys |= found
        elif isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
        else:
            return None
    return keys


def _unstamped_inserts() -> list[str]:
    stamped_cols = _timestamp_columns()
    out: list[str] = []
    for f in sorted((REPO / "app").rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        # map each node to its enclosing function, for the `**spread` lookup
        scopes: dict[int, ast.AST] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(fn):
                    scopes[id(sub)] = fn
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "insert_docs"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "mongo_store"
                    and len(n.args) >= 2
                    and isinstance(n.args[0], ast.Constant)):
                continue
            cols = stamped_cols.get(n.args[0].value)
            if not cols:
                continue
            docs = n.args[1]
            if not (isinstance(docs, ast.List) and docs.elts
                    and isinstance(docs.elts[0], ast.Dict)):
                continue                    # not a literal document
            keys = _literal_keys(docs.elts[0], scopes.get(id(n)))
            if keys is None:
                continue                    # unresolvable spread; not judged
            missing = [c for c in cols if c not in keys]
            if missing:
                out.append(f"{f.relative_to(REPO)}:{n.lineno} "
                           f"insert into {n.args[0].value!r} omits {missing}")
    return sorted(out)


def test_the_defaults_artifact_names_timestamp_columns():
    cols = _timestamp_columns()
    assert len(cols) > 20, "artifact looks wrong — re-run export_pg_column_defaults.py"
    assert "model_shadow_runs" in cols and "created_at" in cols["model_shadow_runs"]


def test_every_literal_insert_stamps_its_timestamp_columns():
    bad = _unstamped_inserts()
    assert bad == [], (
        "Postgres supplied these on INSERT and Mongo supplies nothing, so the "
        "documents drop out of every time-bounded query — no error, just "
        "absent:\n  " + "\n  ".join(bad))


def test_the_scanner_catches_the_shape_and_clears_the_correct_one(tmp_path):
    """Negative control, both directions, plus the `**spread` case."""
    def keys_missing(src: str) -> list[str]:
        tree = ast.parse(src)
        scopes = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(fn):
                    scopes[id(sub)] = fn
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "insert_docs" and len(n.args) >= 2):
                keys = _literal_keys(n.args[1].elts[0], scopes.get(id(n)))
                return sorted({"created_at"} - (keys or set()))
        raise AssertionError("no insert found in fixture")

    bad = ("from app.db import mongo_store\n"
           "def f(x):\n"
           "    mongo_store.insert_docs('t', [{'a': x}])\n")
    assert keys_missing(bad) == ["created_at"]

    good = ("from app.db import mongo_store\n"
            "def f(x, now):\n"
            "    mongo_store.insert_docs('t', [{'a': x, 'created_at': now}])\n")
    assert keys_missing(good) == []

    spread = ("from app.db import mongo_store\n"
              "def f(x, now):\n"
              "    base = {'created_at': now}\n"
              "    mongo_store.insert_docs('t', [{'a': x, **base}])\n")
    assert keys_missing(spread) == [], "a resolvable **spread must count"
