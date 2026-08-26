#!/usr/bin/env python3
"""Backfill decision_scores.created_at from the cycle id's epoch.

The PG table stamped created_at via a column default the Mongo cutover
dropped, so every row written since 2026-08-19 (130 of 442 on 2026-08-26) has
no created_at and is invisible to date-windowed reads — including the attack1
measurement-window census, which needs the score band per decision.

The cycle id carries the truth: `cycle-v3-<unix epoch>`. Derived, not
guessed; rows whose id doesn't parse are reported and left alone.

    python3 scripts/backfill_decision_scores_created_at.py            # dry run
    python3 scripts/backfill_decision_scores_created_at.py --apply

Idempotent: only rows missing created_at are touched.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mongo_store  # noqa: E402

_EPOCH = re.compile(r"-(\d{9,11})$")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    a = ap.parse_args()

    rows = mongo_store.find_docs(
        "decision_scores", {"created_at": None},
        ["cycle_id", "ticker"], limit=100_000,
    )
    if not rows:
        print("No rows missing created_at — nothing to do (vacuity check: "
              f"collection holds {len(mongo_store.find_docs('decision_scores', {}, ['cycle_id'], limit=1))} sample row(s)).")
        return 0

    fixed = skipped = 0
    for r in rows:
        m = _EPOCH.search(str(r.get("cycle_id") or ""))
        if not m:
            skipped += 1
            print(f"  SKIP (no epoch in cycle_id): {r.get('cycle_id')!r} / {r.get('ticker')}")
            continue
        ts = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        if a.apply:
            mongo_store.update_docs(
                "decision_scores",
                {"cycle_id": r["cycle_id"], "ticker": r["ticker"], "created_at": None},
                {"$set": {"created_at": ts}},
            )
        fixed += 1

    mode = "backfilled" if a.apply else "WOULD backfill (dry run — pass --apply)"
    print(f"{mode}: {fixed} row(s); skipped {skipped} unparseable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
