#!/usr/bin/env python3
"""Every non-`app/` file that can reach Postgres, and what we decided about it.

WHY THIS EXISTS
---------------
`tests/unit/test_no_pg_writers_for_trading_data.py` guards WRITES. It was built
on 2026-08-30 after 18 scripts were found still issuing INSERT/UPDATE/DELETE
against the archive, and it works.

Nothing guards READS, and reads are the larger population. The Postgres archive
was frozen on 2026-08-19 and never taken down: it answers, with data that stops
eleven days ago, no error and no staleness marker. `scripts/shadow_report.py` is
the demonstration — it runs clean today and prints rows dated 2026-07-29 citing
`tournament_result`, a subsystem deleted on 08-28.

A write to the wrong store loses data. A read from the wrong store loses a
DECISION, and looks like a report.

The 2026-08-30 audit put the dangerous population at 19 by looking for
`load_dotenv()` + `os.getenv("DATABASE_URL")`. That framing missed two shapes:

  * `scripts/quality_census.pg_url()` falls back to PARSING `.env` off disk when
    the env var is unset, so its importers connect without ever calling
    load_dotenv;
  * four scripts carry a hardcoded production DSN as the `os.getenv` default and
    connect with no `.env` at all.

So this classifier keys on what a file can DO, not on which idiom it uses.

WHAT A ROW MEANS
----------------
Every PG-bound file gets a row with a `disposition`, and the gate test fails on
`unclassified`. The four dispositions:

  mongo         used in current operations — port it, validate against live
                Mongo, leave a test or an executable probe behind
  archive-only  exists to inspect the frozen backup or repair a migration; keep
                the PG requirement explicit and label the output archive-only
  parity-only   a real cross-store parity or PG-quiescence tool; keep, but its
                PG access stays narrowly scoped
  delete        no callers, no runbook reference, obsolete subsystem

`can_connect` is the field to sort by. A file that reaches the DSN only through
`settings.DATABASE_URL` raises AttributeError today (the field was removed
2026-08-28) and is merely broken. A file that reads the environment, or parses
`.env` itself, still works — and answers from July.

USAGE
    python scripts/pg_script_inventory.py               # print the table
    python scripts/pg_script_inventory.py --write       # refresh the JSON
    python scripts/pg_script_inventory.py --unclassified
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INVENTORY = REPO / "docs" / "migration" / "pg_script_inventory.json"

#: Trees scanned. `app/` is deliberately absent: it is held at zero couplings by
#: `scripts/gate_zero_pg.py` and `tests/unit/test_app_image_has_no_pg_driver.py`,
#: both stricter than this and both run before a deploy.
TREES = ("scripts", "tests", ".claude")

SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}

#: This scanner names every DSN environment variable it looks for, and its gate
#: test plants real connection shapes as negative controls — both in CODE rather
#: than prose, so both match the patterns. A scanner that reports itself trains
#: people to ignore the report. They are excluded by PATH rather than by a
#: pattern, so nothing else can slip in behind them.
SKIP_FILES = {
    "scripts/pg_script_inventory.py",
    "tests/unit/test_pg_script_inventory.py",
}

DRIVERS = {"psycopg", "psycopg2", "psycopg_pool", "asyncpg", "pgvector"}

DISPOSITIONS = {"mongo", "archive-only", "parity-only", "delete", "unclassified"}

#: Reads the DSN out of the process environment. These CONNECT.
_ENV_DSN = re.compile(
    r"""os\.(?:environ(?:\.get)?|getenv)\s*[\[(]\s*["']"""
    r"""(?:DATABASE_URL|TEST_DATABASE_URL|PG_ARCHIVE_URL|DB_URL|SIM_DSN)["']"""
)
#: A DSN written into the source. Connects with no configuration at all.
_LITERAL_DSN = re.compile(r"""postgres(?:ql)?(?:\+\w+)?://[^\s"']+""")
#: The shared helper in quality_census that parses `.env` off disk itself.
_PG_URL_HELPER = re.compile(r"\bpg_url\s*\(")


def _iter_files():
    for tree in TREES:
        root = REPO / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.relative_to(REPO).as_posix() in SKIP_FILES:
                continue
            yield path


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def _strip_prose(src: str) -> str:
    """Drop comments and docstrings before looking for DSNs.

    A good part of `scripts/` is prose EXPLAINING what the migration changed —
    `scrub_poisoned_memories` records why a PG-only DELETE was wrong, `wipe_13f`
    quotes the statement it no longer issues. Both are worth keeping and neither
    is a coupling. Same helper shape as the writer guard, for the same reason.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    out = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    for doc in docstrings:
        out = out.replace(doc, "")
    return out


def classify(path: Path) -> dict | None:
    """Return a row for `path`, or None when it has no Postgres surface."""
    src = path.read_text(errors="ignore")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    code = _strip_prose(src)

    imported = set(_imports(tree))
    driver = sorted({m.split(".")[0] for m in imported if m.split(".")[0] in DRIVERS})
    via_pool = any("pg_connection" in m or m == "scripts.migration" for m in imported)
    via_settings = ("settings.DATABASE_URL" in code
                    or "settings.TEST_DATABASE_URL" in code)
    via_env = bool(_ENV_DSN.search(code))
    via_literal = bool(_LITERAL_DSN.search(code))
    via_helper = (bool(_PG_URL_HELPER.search(code))
                  and any("quality_census" in m for m in imported))

    mechanisms = []
    if via_pool:
        mechanisms.append("pg_connection")
    if driver:
        mechanisms.append("driver:" + ",".join(driver))
    if via_settings:
        mechanisms.append("settings.DATABASE_URL")
    if via_env:
        mechanisms.append("env-dsn")
    if via_literal:
        mechanisms.append("literal-dsn")
    if via_helper:
        mechanisms.append("quality_census.pg_url")
    if not mechanisms:
        return None

    # Can this file open a connection TODAY? Anything that reaches the DSN only
    # through `settings.DATABASE_URL` raises AttributeError since 2026-08-28 and
    # is loud. Everything else is quiet, and quiet is worse: it answers from a
    # store frozen on 2026-08-19.
    can_connect = bool(driver) and (via_env or via_literal or via_helper)

    try:
        rel = path.relative_to(REPO).as_posix()
    except ValueError:
        # A file outside the repo — only the negative-control tests do this,
        # and they care about the mechanisms, not the key.
        rel = path.as_posix()

    return {
        "path": rel,
        "mechanisms": mechanisms,
        "can_connect": can_connect,
        "disposition": "unclassified",
        "owner": "",
        "why": "",
    }


def scan() -> list[dict]:
    rows = []
    for path in _iter_files():
        row = classify(path)
        if row:
            rows.append(row)
    return sorted(rows, key=lambda r: r["path"])


def load_inventory() -> dict:
    if INVENTORY.exists():
        return json.loads(INVENTORY.read_text())
    return {"rows": []}


def merge(scanned: list[dict], stored: dict):
    """Carry stored dispositions onto a fresh scan.

    Returns (rows, newly_unclassified, gone). `gone` matters as much as `new`:
    a row whose file was deleted is an allowlist entry outliving its file, which
    is how a guard quietly stops guarding anything.
    """
    by_path = {r["path"]: r for r in stored.get("rows", [])}
    rows, new = [], []
    for row in scanned:
        prior = by_path.get(row["path"])
        if prior:
            row["disposition"] = prior.get("disposition", "unclassified")
            row["owner"] = prior.get("owner", "")
            row["why"] = prior.get("why", "")
        if row["disposition"] == "unclassified":
            new.append(row["path"])
        rows.append(row)
    live = {r["path"] for r in scanned}
    gone = sorted(p for p in by_path if p not in live)
    return rows, new, gone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="refresh the JSON in place")
    ap.add_argument("--unclassified", action="store_true", help="list only new rows")
    args = ap.parse_args()

    rows, new, gone = merge(scan(), load_inventory())

    if args.unclassified:
        for path in new:
            print(path)
        return 1 if new else 0

    counts = Counter(r["disposition"] for r in rows)
    connectable = [r for r in rows if r["can_connect"]]
    print(f"{len(rows)} Postgres-bound files outside app/")
    print(f"  can connect TODAY: {len(connectable)}"
          "   <- the ones that answer from the frozen archive")
    for disp, n in sorted(counts.items()):
        print(f"  {disp:<14} {n}")
    if new:
        print(f"\nUNCLASSIFIED ({len(new)}):")
        for path in new:
            print(f"  {path}")
    if gone:
        print(f"\nSTALE ROWS - file is gone ({len(gone)}):")
        for path in gone:
            print(f"  {path}")

    if args.write:
        INVENTORY.parent.mkdir(parents=True, exist_ok=True)
        INVENTORY.write_text(json.dumps({
            "generated_by": "scripts/pg_script_inventory.py",
            "note": (
                "Every non-app/ file that can reach Postgres. `disposition` is a "
                "HUMAN decision; the scan only finds the files. "
                "tests/unit/test_pg_script_inventory.py fails on `unclassified` "
                "and on a row whose file is gone."
            ),
            "rows": rows,
        }, indent=2) + "\n")
        print(f"\nwrote {INVENTORY.relative_to(REPO)}")

    return 1 if (new or gone) else 0


if __name__ == "__main__":
    sys.exit(main())
