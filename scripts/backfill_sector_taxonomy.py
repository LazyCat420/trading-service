#!/usr/bin/env python3
"""Collapse `ticker_metadata.sector` onto one vocabulary.

    python3 scripts/backfill_sector_taxonomy.py            # dry run (default)
    python3 scripts/backfill_sector_taxonomy.py --apply

Writes only under `--apply`. Read-only otherwise. Safe against a live cycle
(a sector rename cannot change a price or a decision in flight).

`ticker_metadata.sector` held 19 distinct values on 2026-09-12 — GICS off the
static S&P list and Yahoo off yfinance enrichment, writing to the same field.
`app/data/sector_taxonomy.normalise_sector` is now applied at the write seam, so
new rows are canonical; this repairs the rows already stored.

The rename is not cosmetic. `Healthcare` and `Health Care` are one sector under
two labels, so every group-by-sector reported two half-size sectors — and sector
exposure caps and breadth read that.

WHAT IT REFUSES TO DO. A value the taxonomy cannot map becomes **None**, never a
guess at the nearest canonical name. Dropping is recoverable; an invented label
reads as authoritative. Each such row is listed by ticker so the mapping can be
extended deliberately in `_ALIASES` and the backfill re-run.

UNDO. Every change is written to an undo file (`ticker`, `from`, `to`) before
anything is applied.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo-file", default=None)
    ap.add_argument("--collection", default="ticker_metadata")
    args = ap.parse_args()

    from app.data.sector_taxonomy import (CANONICAL_SECTORS, _ABSENT,
                                          normalise_sector)
    from app.db import mongo_store

    rows = mongo_store.find_docs(args.collection, {}, projection={
        "ticker": 1, "symbol": 1, "sector": 1}, limit=0) or []

    changes, unmapped = [], []
    before = collections.Counter()
    after = collections.Counter()
    for r in rows:
        key = r.get("ticker") or r.get("symbol")
        raw = r.get("sector")
        before[str(raw)] += 1
        canon = normalise_sector(raw)
        after[str(canon)] += 1
        if canon == raw:
            continue
        # "Unknown"/"ETF"/"" are DELIBERATE drops — the taxonomy says they mean
        # "no sector", and reporting them as failures would bury the rows that
        # are genuinely unrecognised. Only the latter need a human decision.
        if canon is None and raw not in (None, "") \
                and str(raw).strip().lower() not in _ABSENT:
            unmapped.append((key, raw))
        changes.append({"ticker": key, "from": raw, "to": canon})

    print(f"{args.collection}: {len(rows)} rows")
    print(f"  distinct sector values BEFORE : {len(before)}")
    print(f"  distinct sector values AFTER  : {len(after)}")
    print(f"  rows changed                  : {len(changes)}")
    print("\n  before -> after, for the values that move:")
    moved = collections.Counter((str(c["from"]), str(c["to"])) for c in changes)
    for (f, t), n in moved.most_common(20):
        print(f"     {n:>5}  {f:<26} -> {t}")
    absent_drops = sum(1 for c in changes
                       if c["to"] is None and c["from"] not in (None, ""))
    print(f"\n  deliberate drops to None (Unknown/ETF/blank): "
          f"{absent_drops - len(unmapped)}")
    if unmapped:
        print(f"\n  !! {len(unmapped)} row(s) hold a sector this taxonomy does NOT "
              f"recognise; they become None:")
        for k, v in unmapped[:15]:
            print(f"       {k:<8} {v!r}")
        print("     Add a real synonym to sector_taxonomy._ALIASES and re-run, "
              "or accept the drop.")

    leftover = {s for s in after if s not in ("None",)} - set(CANONICAL_SECTORS)
    if leftover:
        print(f"\n  !! would still hold non-canonical values: {sorted(leftover)}")
        return 1

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    undo_path = Path(args.undo_file or
                     f"sector_taxonomy_undo_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    undo_path.write_text(json.dumps(changes, indent=1, default=str))
    print(f"\nundo written to {undo_path} ({len(changes)} rows)")

    for c in changes:
        mongo_store.update_docs(args.collection, {"ticker": c["ticker"]},
                                {"$set": {"sector": c["to"]}})
    print(f"updated {len(changes)} rows")
    print("Verify: the real_mongo guard in "
          "tests/unit/test_sector_taxonomy_is_one_vocabulary.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
