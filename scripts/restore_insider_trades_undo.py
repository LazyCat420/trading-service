#!/usr/bin/env python3
"""Put back the documents `scripts/dedupe_insider_trades.py --apply` removed.

    python3 scripts/restore_insider_trades_undo.py undo.json            # dry run
    python3 scripts/restore_insider_trades_undo.py undo.json --apply

WHY THIS FILE EXISTS
--------------------
`scripts/dedupe_asset_prices.py` says its undo file "restores the exact
documents". It does not, twice over:

  * it writes with `json.dumps(losers, default=str)`, which turns `_id` into a
    str and `date` into a str. A restored document's `_id` is then a STRING
    that cannot collide with the original ObjectId, so a partial restore
    silently DOUBLES the collection instead of erroring; and
  * `grep -rn undo scripts/` finds no consumer anywhere — the restore path was
    never written.

So the dedupe script writes `bson.json_util`, and this is the consumer. It
refuses a file whose `_id`s are strings, and it refuses to insert any `_id`
already present rather than writing a partial double.

A restore is a repair, not a rollback of the index: if
`dedupe_insider_trades.py --build-index` already built the unique index, this
will fail on the duplicate keys it is trying to re-create. Drop
`natural_key_unique` first if that is genuinely what you want.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TABLE = "insider_trades"

EXIT_OK = 0
EXIT_REFUSED = 1


def _collection():
    from app.db.collections import collection_for
    from app.db.mongo_store import get_doc_db

    return get_doc_db()[collection_for(TABLE)]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("undo_file")
    ap.add_argument("--apply", action="store_true",
                    help="actually insert (default is a dry run)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    from bson import ObjectId, json_util

    payload = json_util.loads(Path(args.undo_file).read_text())
    table = payload.get("table")
    docs = payload.get("documents") or []

    if table != TABLE:
        print(f"REFUSED: undo file is for {table!r}, not {TABLE!r}.", file=sys.stderr)
        return EXIT_REFUSED

    if payload.get("count") is not None and payload["count"] != len(docs):
        print(f"REFUSED: header says {payload['count']} documents, file holds "
              f"{len(docs)} — truncated or edited.", file=sys.stderr)
        return EXIT_REFUSED

    if not docs:
        print("undo file holds no documents — nothing to restore.")
        return EXIT_OK

    bad = [d for d in docs if not isinstance(d.get("_id"), ObjectId)]
    if bad:
        print(f"REFUSED: {len(bad)} of {len(docs)} documents have a non-ObjectId "
              f"`_id` (e.g. {bad[0].get('_id')!r}). This file was written with "
              f"json.dumps(default=str), not bson.json_util; inserting it would "
              f"create documents that can never match the originals and would "
              f"DOUBLE the collection.", file=sys.stderr)
        return EXIT_REFUSED

    coll = _collection()
    ids = [d["_id"] for d in docs]
    present = 0
    for i in range(0, len(ids), 1000):
        present += coll.count_documents({"_id": {"$in": ids[i:i + 1000]}})

    print(f"{TABLE}  (physical collection: {coll.name})")
    print(f"  documents now              {coll.count_documents({})}")
    print(f"  documents in the undo file {len(docs)}")
    print(f"  of those, ALREADY PRESENT  {present}")

    if present:
        print(f"\nREFUSED: {present} of the {len(docs)} `_id`s are already in the "
              f"collection. Inserting would be a partial write, which is how a "
              f"restore doubles a collection. Nothing written.", file=sys.stderr)
        return EXIT_REFUSED

    if not args.apply:
        print("\nDRY RUN — nothing inserted. Re-run with --apply.")
        return EXIT_OK

    inserted = 0
    for i in range(0, len(docs), 1000):
        inserted += len(coll.insert_many(docs[i:i + 1000], ordered=False).inserted_ids)
    print(f"\ninserted {inserted} documents")
    print(f"documents now {coll.count_documents({})}")
    return EXIT_OK if inserted == len(docs) else EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
