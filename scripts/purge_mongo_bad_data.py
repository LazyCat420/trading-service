#!/usr/bin/env python3
"""Apply the same quality gates to MongoDB. DESTRUCTIVE with --execute.

Why this exists: the live mirror had already copied rows into Mongo before the
Postgres purge ran, so `purge_bad_data.py` cleaning Postgres alone left the bad
documents sitting in the store we are migrating TO — 183,712 of them. Purging
one side of a mirrored pair is not purging.

Gates come from `quality_gates.py`, the same file the Postgres purge uses. Each
carries a `mongo` query document written alongside its SQL predicate.

The decisive check is not that the deletes ran. It is that afterwards, for
every gated table, the Postgres row count and the Mongo document count are
EQUAL. Two independently written predicates agreeing on a count is evidence
they mean the same thing; a delete that merely completes is not.

Tables that also drop documents the mirror never wrote (or is behind on) show
up as a remaining delta, and are reported rather than hidden.

Usage:
    python scripts/purge_mongo_bad_data.py
    python scripts/purge_mongo_bad_data.py --execute
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.db import mongo_store  # noqa: E402
from scripts import quality_gates as QG  # noqa: E402
from scripts.quality_census import pg_url  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"


def resolve(query):
    """Replace the "NOW" sentinel with the current UTC time.

    The gate is a literal written at import time; a datetime baked in there
    would freeze at whenever the module was first loaded.
    """
    if isinstance(query, dict):
        return {k: resolve(v) for k, v in query.items()}
    if isinstance(query, list):
        return [resolve(v) for v in query]
    if query == "NOW":
        return datetime.now(timezone.utc)
    return query


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    gates = [g for g in QG.ROW_GATES if g.mongo is not None]
    missing = [g for g in QG.ROW_GATES if g.mongo is None]
    QG.assert_no_foreign([g.table for g in gates])

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"=== {mode} — Mongo side ===")
    if missing:
        # A gate with no Mongo form is not a gate that found nothing; it is a
        # gate that never ran. Say so.
        print(f"\n{len(missing)} gate(s) have NO Mongo predicate and are NOT applied:")
        for g in missing:
            print(f"  {g.table}.{g.name}")

    results = []
    print(f"\n{'TABLE':<24} {'GATE':<22} {'MATCHED':>9} {'DELETED':>9}")
    for g in gates:
        q = resolve(g.mongo)
        try:
            n = mongo_store.count_docs(g.table, q)
        except Exception as exc:
            print(f"{g.table:<24} {g.name:<22}  ERROR {type(exc).__name__}: {exc}"[:110])
            results.append({"table": g.table, "gate": g.name, "error": str(exc)[:200]})
            continue
        deleted = 0
        if args.execute and n:
            deleted = mongo_store.delete_docs(g.table, q)
        print(f"{g.table:<24} {g.name:<22} {n:>9,} {deleted:>9,}")
        results.append({"table": g.table, "gate": g.name,
                        "matched": n, "deleted": deleted})

    # The check that matters: both stores must now agree, table by table.
    tables = sorted({g.table for g in QG.ROW_GATES})
    print(f"\n{'TABLE':<24} {'POSTGRES':>10} {'MONGO':>10} {'DELTA':>9}")
    parity, deltas = True, {}
    with psycopg.connect(pg_url(), connect_timeout=30) as conn:
        cur = conn.cursor()
        for t in tables:
            cur.execute(f'SELECT count(*) FROM "{t}"')
            pg_n = cur.fetchone()[0]
            mg_n = mongo_store.count_docs(t)
            delta = mg_n - pg_n
            deltas[t] = {"postgres": pg_n, "mongo": mg_n, "delta": delta}
            if delta:
                parity = False
            print(f"{t:<24} {pg_n:>10,} {mg_n:>10,} {delta:>+9,}"
                  + ("" if delta == 0 else "   <<<"))

    print(f"\nstores agree on every gated table: {parity}")
    if not parity:
        print("A non-zero delta is not necessarily a purge failure — a table the "
              "backfill has not reached yet will read low. Re-run after the sweep.")

    if args.execute:
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / "purge_mongo_report.json").write_text(json.dumps({
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "gates": results, "parity": parity, "counts": deltas,
        }, indent=2))
        print(f"\nwrote {REPORTS/'purge_mongo_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
