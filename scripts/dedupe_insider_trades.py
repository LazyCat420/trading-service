#!/usr/bin/env python3
"""Remove the duplicate rows in `insider_trades`, keeping the FIRST write per `id`.

    python3 scripts/dedupe_insider_trades.py                     # dry run (default)
    python3 scripts/dedupe_insider_trades.py --apply --undo-file undo.json
    python3 scripts/dedupe_insider_trades.py --apply --build-index

DESTRUCTIVE under `--apply`. Take a mongodump first (the header the dry run
prints spells the exact command); the undo file is recoverable by
`scripts/restore_insider_trades_undo.py`, but a dump is the cheaper insurance.

WHY THERE ARE DUPLICATES
------------------------
`app/collectors/openinsider_collector.py` wrote with `insert_docs` under a
comment that is a character-for-character clone of the `asset_prices` one:
*"ON CONFLICT (id) DO NOTHING — insert_docs (ordered=False) swallows
duplicate-key errors, which is that semantic."*

Postgres enforced that key. The Mongo `natural_key` index on `(id, 1)` was
created WITHOUT `unique`, so the server never raises a duplicate-key error,
there is nothing to swallow, and every collector pass appends another copy.
Measured live 2026-09-12:

    insider_trades ... 2,446 documents for 356 distinct ids (85.4% redundant)
                       45 ids stored 23 times each
    indexes .......... _id_, and `natural_key` on (id, 1) — NOT unique

The collector now writes `bulk_upsert(..., key_field="id", insert_only=True)`,
the idiom `polygon_collector.py` and `news_collector.py` already use, which is
DO NOTHING by construction rather than by dependence on an index.

WHICH COPY SURVIVES, AND WHY THE CHOICE IS ALMOST A NON-ISSUE
-------------------------------------------------------------
FIRST by `_id`. Two measurements, both against the live collection:

  * The copies are IDENTICAL on all twelve business fields. Of the 188 ids
    holding more than one copy, ZERO differ on any of ticker, insider_name,
    insider_title, trade_type, price, qty, value, shares_owned, delta_pct,
    trade_date, filing_date or source. The trade content is byte-identical,
    so for the trade itself "keep any" would have been correct — unlike
    `asset_prices`, where 32 of 196 archived days carried disagreeing closes.

  * The ONE field that differs is `collected_at`, and it differs by PRESENCE:
    246 of 2,446 documents carry it, and every one of those is a first-by-
    `_id`. Against the Postgres archive (`scripts/quality_census.pg_url()`,
    246 rows, 246 distinct ids, all present in Mongo), first-by-`_id`
    reproduces the archive 246/246 on EVERY column including `collected_at`;
    last-by-`_id` matches 246/246 on the business columns but only 154/246 on
    `collected_at`.

So first-by-`_id` is not a guess and not an inherited convention: it is the
only rule that does not throw away the original observation timestamp. This
script RE-MEASURES both facts on every run and prints them, so a later run
against different data cannot quietly inherit the claim.

THE INDEX IS THE REAL FIX. `--build-index` DROPS the non-unique `natural_key`
and creates the unique one. It drops first because `create_index` on the same
key pattern with different options RAISES (IndexOptionsConflict) — it does not
modify — which is the bug that leaves `dedupe_asset_prices.py` with a
traceback, an unreachable warning, and a collection that is deduped and
unprotected. If the build fails here the script says DEDUPED AND UNPROTECTED
and exits non-zero.

The unique index is deliberately NOT declared in `mongo_store.ensure_indexes`:
that declaration must be added only once the data is clean, because
`ensure_indexes._try` swallows the failure and a deploy would report success
over an unprotected collection.
"""

from __future__ import annotations

import argparse
import collections
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TABLE = "insider_trades"
KEY = ("id",)
INDEX_NAME = "natural_key_unique"

# Business columns, i.e. the trade. `collected_at` is deliberately NOT here:
# it is the observation timestamp, and it is the only field that varies.
BUSINESS_FIELDS = (
    "ticker", "insider_name", "insider_title", "trade_type", "price", "qty",
    "value", "shares_owned", "delta_pct", "trade_date", "filing_date", "source",
)

EXIT_OK = 0
EXIT_SOMETHING_WRONG = 1
EXIT_NOTHING_TO_DO = 2
EXIT_UNPROTECTED = 3


def _collection():
    """The physical collection for the POSTGRES TABLE NAME, through the
    resolver. Never `get_doc_db()["insider_trades"]`: Mongo creates a
    collection on first touch, so a name that bypasses `collection_for` does
    not error, it silently opens a second, invisible collection."""
    from app.db.collections import collection_for
    from app.db.mongo_store import get_doc_db

    return get_doc_db()[collection_for(TABLE)]


def _key(doc: dict[str, Any]) -> tuple:
    return tuple(doc.get(f) for f in KEY)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run)")
    ap.add_argument("--build-index", action="store_true",
                    help="after deduping, drop the non-unique natural_key and "
                         "build the UNIQUE one")
    ap.add_argument("--undo-file", default=None,
                    help="where to write the losing documents (bson json_util; "
                         "replayable by scripts/restore_insider_trades_undo.py)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    coll = _collection()

    # Exact, not estimated. `estimated_document_count()` is a metadata read
    # that can be stale or wrong after an unclean shutdown, and comparing an
    # estimate for EQUALITY against an exact distinct-key count either reports
    # a mismatch that is not real or hides one that is.
    total = coll.count_documents({})

    # Grouped in the client rather than with $group so the survivor is chosen
    # by _id ORDER — the rule the archive validates — and so the losing
    # documents are captured in full for the undo file.
    seen: dict[tuple, dict] = {}
    losers: list[dict] = []
    per_key: collections.Counter = collections.Counter()
    copies: dict[tuple, list[dict]] = collections.defaultdict(list)
    for doc in coll.find({}, sort=[("_id", 1)]):
        k = _key(doc)
        per_key[k] += 1
        copies[k].append(doc)
        if k in seen:
            losers.append(doc)
        else:
            seen[k] = doc

    dupe_keys = sum(1 for v in per_key.values() if v > 1)

    # Re-measure the survivor-rule premise instead of asserting it.
    business_disagreeing = 0
    varying_fields: collections.Counter = collections.Counter()
    for k, ds in copies.items():
        if len(ds) < 2:
            continue
        if len({tuple(repr(d.get(f)) for f in BUSINESS_FIELDS) for d in ds}) > 1:
            business_disagreeing += 1
        fields = set().union(*(set(d) for d in ds)) - {"_id"}
        for f in fields:
            if len({repr(d.get(f)) for d in ds}) > 1:
                varying_fields[f] += 1

    first_has_collected = sum(1 for ds in copies.values() if ds[0].get("collected_at") is not None)
    last_has_collected = sum(1 for ds in copies.values() if ds[-1].get("collected_at") is not None)

    print(f"{TABLE}  (physical collection: {coll.name})")
    print(f"  documents                      {total}")
    print(f"  distinct natural keys ({'+'.join(KEY)})     {len(seen)}")
    print(f"  redundant documents            {len(losers)}"
          + (f"  ({100.0 * len(losers) / total:.1f}%)" if total else ""))
    print(f"  keys with >1 copy              {dupe_keys}")
    print(f"  ...copies disagreeing on any BUSINESS field: {business_disagreeing}"
          f" of {dupe_keys}")
    print(f"  fields that ever vary across copies: "
          f"{dict(varying_fields) or '(none)'}")
    print(f"  collected_at present on first-by-_id: {first_has_collected}/{len(seen)}"
          f"   on last-by-_id: {last_has_collected}/{len(seen)}")
    print("  worst offenders:")
    for k, n in per_key.most_common(5):
        print(f"     {n:>5} copies  id={k[0]}")

    if not losers:
        print("\nnothing to do — every natural key holds exactly one document.")
        if args.build_index:
            return _build_index(coll, deduped=False)
        return EXIT_NOTHING_TO_DO

    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply.")
        print(f"Survivor rule: FIRST by _id. Business fields are identical across "
              f"copies ({business_disagreeing} of {dupe_keys} disagree), so the "
              f"trade content makes the choice a non-issue; first-by-_id is "
              f"chosen because it is the only copy carrying `collected_at` "
              f"({first_has_collected} vs {last_has_collected}), and it "
              f"reproduces the Postgres archive 246/246 where last-by-_id "
              f"reproduces it 154/246.")
        print("\nBefore --apply, a human must run:")
        print(f"  mongodump --uri \"$MONGO_URI\" --db trading_bot "
              f"--collection {coll.name} --out insider_trades_dump_$(date -u +%Y%m%dT%H%M%SZ)")
        return EXIT_OK

    undo_path = Path(args.undo_file or
                     f"{TABLE}_undo_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    _write_undo(undo_path, losers)
    print(f"\nundo written to {undo_path} ({len(losers)} full documents, bson json_util)")
    print(f"  replay with: python3 scripts/restore_insider_trades_undo.py {undo_path} --apply")

    ids = [d["_id"] for d in losers]
    deleted = 0
    for i in range(0, len(ids), 1000):
        deleted += coll.delete_many({"_id": {"$in": ids[i:i + 1000]}}).deleted_count
    print(f"deleted {deleted} redundant documents")

    remaining = coll.count_documents({})
    print(f"documents now {remaining} (expected {len(seen)})")
    if remaining != len(seen):
        print("!! count does not match the distinct-key count — STOP and inspect",
              file=sys.stderr)
        return EXIT_SOMETHING_WRONG

    if args.build_index:
        return _build_index(coll, deduped=True)

    print("index NOT built. Re-run with --build-index, or the collection is "
          "still unprotected at the storage layer.")
    return EXIT_OK


def _write_undo(path: Path, docs: list[dict]) -> None:
    """`bson.json_util` — NOT `json.dumps(..., default=str)`.

    default=str turns `_id` into a str and `trade_date` into a str. A restored
    document's `_id` is then a string that cannot collide with the original
    ObjectId, so a partial restore silently DOUBLES the collection instead of
    erroring. The idiom is already used at `scripts/learning_migrate.py:13`.
    """
    from bson import json_util

    path.write_text(json_util.dumps(
        {"table": TABLE,
         "written_at": datetime.now(timezone.utc).isoformat(),
         "survivor_rule": "first by _id",
         "count": len(docs),
         "documents": docs},
        indent=1))


def _build_index(coll, *, deduped: bool) -> int:
    """Drop the non-unique natural_key, then create the unique one.

    `create_index` with different options on an identical key pattern RAISES
    (IndexOptionsConflict); it does not modify. `mongo_store._drop_ttl` shows
    the drop-then-create idiom. Without the drop the operator gets a traceback
    AFTER the deletes and the collection is left deduped and unprotected —
    exactly the window in which the collector starts re-appending.
    """
    keys = [(f, 1) for f in KEY]
    try:
        for info in coll.list_indexes():
            name = info.get("name")
            if name in ("_id_", INDEX_NAME):
                continue
            if [tuple(k) for k in info["key"]] == [tuple(k) for k in keys]:
                coll.drop_index(name)
                print(f"dropped non-unique index {name} on {list(KEY)}")
        coll.create_index(keys, name=INDEX_NAME, unique=True)
    except Exception as exc:
        print(f"!! unique index build FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        if deduped:
            print("!! the collection is DEDUPED AND UNPROTECTED — a collector pass "
                  "can start appending duplicates again. Build the index by hand "
                  "before the next cycle, or restore from the undo file.",
                  file=sys.stderr)
        else:
            print("!! the collection is UNPROTECTED (no duplicates were present, "
                  "none were deleted).", file=sys.stderr)
        return EXIT_UNPROTECTED

    print(f"built UNIQUE index {INDEX_NAME} — a duplicate can no longer be written.")
    print("Now (and only now) the declaration may be added to "
          "mongo_store.ensure_indexes; until the data is clean, _try swallows "
          "the failure and a deploy reports success over an unprotected collection.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
