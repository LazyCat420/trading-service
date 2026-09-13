#!/usr/bin/env python3
"""Remove the duplicate bars in `asset_prices`, keeping the FIRST write per day.

    python3 scripts/dedupe_asset_prices.py                    # dry run (default)
    python3 scripts/dedupe_asset_prices.py --apply --undo-file undo.json
    python3 scripts/dedupe_asset_prices.py --apply --build-index

DESTRUCTIVE under `--apply`. Take a mongodump first; the undo file restores the
exact documents, but a dump is the cheaper insurance.

WHY THERE ARE DUPLICATES
------------------------
`app/collectors/market_regime_collector.py:200` writes with `insert_docs` under
the comment: *"ON CONFLICT (symbol, asset_class, date) DO NOTHING — insert_docs
is ordered=False and swallows duplicate-key errors, which is DO NOTHING."*

Postgres enforced that key. The Mongo collection's `natural_key` index was
created WITHOUT `unique`, so there is no duplicate-key error to swallow and
every collector pass appends another copy of every bar it fetched. Measured:

    GSPC ............ 4,203 documents over 203 sessions (33 copies of 2026-03-02)
    collection ...... 133,000 documents for 6,956 natural keys

WHICH COPY SURVIVES, AND WHY IT IS NOT ARBITRARY
------------------------------------------------
The FIRST document written for a day, by `_id`. That is what the writer intended
(DO NOTHING keeps the incumbent) and it is what the Postgres archive holds:
first-by-`_id` reproduces the archive on 196/196 GSPC days and 200/200 VIX days,
where last-by-`_id` matches only 164 and 154 (`scripts/grade_regime_calls.py:81`).

This matters because the copies DISAGREE — 32 of GSPC's 196 archived days carry
more than one distinct close — so "keep any" is not a rounding choice.

THE INDEX IS THE REAL FIX. `--build-index` adds the unique index the collection
should always have had, so a duplicate cannot be written again and the writer's
DO-NOTHING comment becomes true. It is a separate flag because it must run AFTER
the dedupe (a unique build fails while duplicates exist) and because building it
on a live collection is a deliberate act.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

KEY = ("symbol", "asset_class", "date")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--build-index", action="store_true",
                    help="after deduping, add the UNIQUE natural-key index")
    ap.add_argument("--undo-file", default=None)
    args = ap.parse_args()

    from app.db.mongo_store import get_doc_db

    coll = get_doc_db()["asset_prices"]
    total = coll.estimated_document_count()

    # Grouped in the client rather than with $group so the survivor is chosen by
    # _id ORDER, which is the rule the archive validates, and so the losing
    # documents can be captured in full for the undo file.
    seen: dict[tuple, dict] = {}
    losers: list[dict] = []
    per_key = collections.Counter()
    for doc in coll.find({}, sort=[("_id", 1)]):
        k = tuple(doc.get(f) for f in KEY)
        per_key[k] += 1
        if k in seen:
            losers.append(doc)
        else:
            seen[k] = doc

    dupe_keys = sum(1 for v in per_key.values() if v > 1)
    worst = per_key.most_common(3)
    disagreeing = 0
    by_key_closes: dict[tuple, set] = collections.defaultdict(set)
    for doc in losers:
        k = tuple(doc.get(f) for f in KEY)
        by_key_closes[k].add(round(float(doc["close"]), 6) if doc.get("close") is not None else None)
    for k, closes in by_key_closes.items():
        keep = seen[k].get("close")
        keep = round(float(keep), 6) if keep is not None else None
        if closes - {keep}:
            disagreeing += 1

    print("asset_prices")
    print(f"  documents            {total}")
    print(f"  distinct natural keys{len(seen):>8}")
    print(f"  redundant documents  {len(losers)}")
    print(f"  keys with >1 copy    {dupe_keys}")
    print(f"  ...of those, copies DISAGREE on close: {disagreeing}")
    print("  worst offenders:")
    for k, n in worst:
        print(f"     {n:>5} copies  {k[0]} {k[1]} {str(k[2])[:10]}")

    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply.")
        print("Survivor rule: FIRST by _id (reproduces the PG archive 196/196).")
        return 0

    undo_path = Path(args.undo_file or
                     f"asset_prices_undo_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    undo_path.write_text(json.dumps(losers, indent=1, default=str))
    print(f"\nundo written to {undo_path} ({len(losers)} full documents)")

    ids = [d["_id"] for d in losers]
    deleted = 0
    for i in range(0, len(ids), 1000):
        deleted += coll.delete_many({"_id": {"$in": ids[i:i + 1000]}}).deleted_count
    print(f"deleted {deleted} redundant documents")

    remaining = coll.estimated_document_count()
    print(f"documents now {remaining} (expected {len(seen)})")
    if remaining != len(seen):
        print("!! count does not match the distinct-key count — STOP and inspect",
              file=sys.stderr)
        return 1

    if args.build_index:
        coll.create_index([(f, 1) for f in KEY], name="natural_key_unique", unique=True)
        print("built UNIQUE index natural_key_unique — a duplicate can no longer "
              "be written, and the collector's DO-NOTHING comment is now true")
    else:
        print("index NOT built. Re-run with --build-index, or the collector will "
              "start appending duplicates again on its next pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
