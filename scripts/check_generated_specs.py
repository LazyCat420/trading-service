#!/usr/bin/env python3
"""Compare generated table specs against the hand-written ones, on real rows.

The generator (app/db/table_spec.py) has to earn its place: for every table
that already has a hand-written entry, it must produce the SAME document, or
the difference must be a known, listed one. Read-only — it fetches a few rows
and compares in memory; it writes to neither store.

    PYTHONPATH=. python scripts/check_generated_specs.py [--rows N]
"""
import argparse
import sys
from datetime import datetime

sys.path.insert(0, ".")

from app.db import connection, table_spec  # noqa: E402
from scripts.pg_to_mongo_backfill import TABLES  # noqa: E402


def compare(table: str, rows: int) -> tuple[str, list[str]]:
    """Return (verdict, differences) for one table."""
    hand_sql, hand_key, hand_map = TABLES[table]
    try:
        with connection.get_db() as db:
            gen_sql, gen_key, gen_map = table_spec.spec_for(table, db)
    except (KeyError, ValueError) as exc:
        return "NEEDS-OVERRIDE", [str(exc)]

    diffs: list[str] = []
    if gen_key != hand_key:
        diffs.append(f"key: hand={hand_key!r} generated={gen_key!r}")

    with connection.get_db() as db:
        cur = db.execute(f"{hand_sql} ORDER BY {hand_key} DESC LIMIT %s", [rows])
        hand_rows = cur.fetchall()
        hand_cols = [c[0] for c in cur.description]
    if not hand_rows:
        return "NO-ROWS", ["table is empty — comparison proves nothing"]

    with connection.get_db() as db:
        cur = db.execute(f"{gen_sql} ORDER BY {gen_key} DESC LIMIT %s", [rows])
        gen_rows = cur.fetchall()
        gen_cols = [c[0] for c in cur.description]

    hand_docs = {d[hand_key]: d for d in (hand_map(r, hand_cols) for r in hand_rows)}
    gen_docs = {d[gen_key]: d for d in (gen_map(r, gen_cols) for r in gen_rows)}

    for key, hand_doc in hand_docs.items():
        gen_doc = gen_docs.get(key)
        if gen_doc is None:
            diffs.append(f"key {key!r}: generated spec returned no row")
            continue
        only_hand = set(hand_doc) - set(gen_doc)
        only_gen = set(gen_doc) - set(hand_doc)
        if only_hand:
            diffs.append(f"fields only the hand mapper emits: {sorted(only_hand)}")
        if only_gen:
            diffs.append(f"fields only the generator emits: {sorted(only_gen)}")
        for field in set(hand_doc) & set(gen_doc):
            a, b = hand_doc[field], gen_doc[field]
            if isinstance(a, datetime) and isinstance(b, datetime):
                if a != b:
                    diffs.append(f"field {field!r} differs: {a!r} vs {b!r}")
            elif a != b:
                diffs.append(f"field {field!r} differs: {a!r} vs {b!r}")
        break  # one row is enough to expose a shape difference

    # de-duplicate, keep order
    seen, uniq = set(), []
    for d in diffs:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return ("AGREES" if not uniq else "DIFFERS"), uniq


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=3)
    args = ap.parse_args()

    agree, differ, other = [], [], []
    for table in TABLES:
        verdict, diffs = compare(table, args.rows)
        print(f"[{table}] {verdict}")
        for d in diffs:
            print(f"    {d}")
        (agree if verdict == "AGREES" else differ if verdict == "DIFFERS" else other).append(table)

    print(f"\nagrees with the hand-written mapper: {len(agree)}/{len(TABLES)}")
    if differ:
        print(f"differs (needs an explicit override): {', '.join(differ)}")
    if other:
        print(f"inconclusive: {', '.join(other)}")
    # Differences are information, not failure: the point is to know exactly
    # which tables the generator cannot serve.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
