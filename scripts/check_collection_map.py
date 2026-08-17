#!/usr/bin/env python3
"""Validate app/db/collection_map.json against the migration ledger.

The map is hand-authored, so it needs a machine to keep it honest. Four things
can go wrong, and only the first is obvious:

  coverage    a table added to Postgres that nobody named
  injectivity two tables pointing at one collection -- which in Mongo does not
              error, it silently merges two entities into one collection
  grammar     a name outside the six access-pattern prefixes, or carrying a
              version token, which is how the current mess started (`v3_`)
  drift       the map disagreeing with the ledger about which tables exist

Exit codes: 0 valid, 1 invalid, 2 a file is missing or unreadable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_PREFIXES = ("log_", "state_", "ts_", "ref_", "q_", "ledger_")
NAME_RE = re.compile(r"^(log|state|ts|ref|q|ledger)_[a-z][a-z0-9_]{2,44}$")
# A version token in a name is a promise to rename it again later.
VERSION_RE = re.compile(r"(^|_)v[0-9]+(_|$)")


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)

    map_path = REPO_ROOT / "app" / "db" / "collection_map.json"
    ledger_path = REPO_ROOT / "app" / "db" / "migration_ledger.json"
    for p in (map_path, ledger_path):
        if not p.exists():
            print(f"FAIL: {p} is missing", file=sys.stderr)
            return 2
    try:
        cmap = json.loads(map_path.read_text(encoding="utf-8"))
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    entries = cmap["collections"]
    problems: list[str] = []

    # 1. Coverage: exactly the ledger's `migrate` tables, no more, no less.
    migrate = {t["table"] for t in ledger["tables"] if t["disposition"] == "migrate"}
    missing = migrate - set(entries)
    extra = set(entries) - migrate
    for t in sorted(missing):
        problems.append(f"{t}: disposition=migrate in the ledger but absent from the map")
    for t in sorted(extra):
        problems.append(f"{t}: in the map but not a `migrate` table in the ledger")

    # 2. Injectivity. In Mongo two tables sharing a collection name does not
    #    error -- it merges two entities, silently, forever.
    by_collection: dict[str, list[str]] = {}
    for table, e in entries.items():
        by_collection.setdefault(e["collection"], []).append(table)
    for coll, tables in sorted(by_collection.items()):
        if len(tables) > 1:
            problems.append(f"{coll}: claimed by {len(tables)} tables: {sorted(tables)}")

    # 3. Grammar.
    for table, e in sorted(entries.items()):
        name = e["collection"]
        if not name.startswith(VALID_PREFIXES):
            problems.append(f"{table} -> {name}: no access-pattern prefix {VALID_PREFIXES}")
        elif not NAME_RE.match(name):
            problems.append(f"{table} -> {name}: does not match {NAME_RE.pattern}")
        if VERSION_RE.search(name):
            problems.append(f"{table} -> {name}: carries a version token")

    # 4. A prefix that disagrees with the ledger shape must say why. Shapes are
    #    known to be unstable (the classifier reads sibling working trees), so
    #    this is a "state your reason" rule, not a hard equality.
    implied = {"append": "log_", "mutable": "state_", "upsert": "ts_",
               "timeseries": "ts_", "reference": "ref_", "queue": "q_",
               "money": "ledger_"}
    for table, e in sorted(entries.items()):
        want = implied.get(e.get("shape_at_authoring", ""))
        if want and not e["collection"].startswith(want) and not e.get("rename_reason"):
            problems.append(
                f"{table} -> {e['collection']}: shape {e['shape_at_authoring']!r} implies "
                f"{want!r}; add a rename_reason if that is deliberate"
            )

    # 5. Money discipline: ledger_* is the one prefix that carries a policy.
    for table, e in sorted(entries.items()):
        is_ledger = e["collection"].startswith("ledger_")
        policy = e.get("numeric_policy")
        if is_ledger and policy != "dec128":
            problems.append(f"{table} -> {e['collection']}: ledger_* must be dec128, got {policy!r}")
        if not is_ledger and policy == "dec128":
            problems.append(f"{table} -> {e['collection']}: dec128 outside ledger_*")

    if problems:
        print(f"FAIL: {len(problems)} problem(s) in collection_map.json:")
        for p in problems:
            print(f"  - {p}")
        return 1

    active = cmap.get("apply_renames", False)
    print(
        f"OK: {len(entries)} tables -> {len(by_collection)} unique collections; "
        f"renames {'ACTIVE' if active else 'inert'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
