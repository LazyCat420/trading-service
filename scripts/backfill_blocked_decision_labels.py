#!/usr/bin/env python
"""Label the policy-blocked decisions that landed unlabelled.

`overridden_from` is the only thing separating a trade the policy gate refused
from one the desk kept — `override_scorecard()` buckets on it, and the row
deliberately keeps `action='BUY'` because its P&L is the counterfactual the
confidence floor is back-tested with.

Until 2026-08-06 the label was read only from `trade_results.policy_action`,
and that row is not always written. The blocks whose row was missing were
recorded as trades the desk kept, and five had already been graded.

This backfills them from `v3_guardrail_firings`, which the guardrail writes on
the path that refuses the trade. Only `overridden_from` is touched: `action`,
`outcome` and `pnl_pct` keep their meaning as the counterfactual.

    python scripts/backfill_blocked_decision_labels.py --dry-run
    python scripts/backfill_blocked_decision_labels.py --apply

`--apply` writes `backfill_blocked_labels_undo.json` beside the repo root
before touching anything. To revert, feed those ids back with
`UPDATE decision_outcomes SET overridden_from = NULL WHERE id IN (...)`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_store  # noqa: E402

def _unlabelled() -> list[dict]:
    """decision_outcomes JOIN v3_guardrail_firings on (cycle_id, ticker), for
    policy blocks that carry no label yet.

    Two-column join, so it is stitched here rather than through
    mongo_query.join_rows (one equality key by design). LIKE
    'HOLD_POLICY_BLOCKED%' becomes an anchored $regex; `overridden_from IS
    NULL` becomes $in [None] so a MISSING field counts as unlabelled too,
    which is what IS NULL meant on a column that was often never written.
    """
    firings = {}
    for g in mongo_store.find_docs(
        "v3_guardrail_firings",
        {"guardrail": {"$regex": "^HOLD_POLICY_BLOCKED"}},
    ):
        firings.setdefault((g.get("cycle_id"), g.get("ticker")), g)

    out = []
    for d in mongo_store.find_docs(
        "decision_outcomes", {"overridden_from": {"$in": [None]}},
        sort=[("created_at", 1)],
    ):
        g = firings.get((d.get("cycle_id"), d.get("ticker")))
        if g is None:
            continue  # no match, no row -- INNER JOIN
        out.append({
            "id": d.get("id"), "cycle_id": d.get("cycle_id"),
            "ticker": d.get("ticker"), "action": d.get("action"),
            "confidence": d.get("confidence"),
            "overridden_from": d.get("overridden_from"),
            "outcome": d.get("outcome"), "pnl_pct": d.get("pnl_pct"),
            "guardrail": g.get("guardrail"),
        })
    return out

UNDO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backfill_blocked_labels_undo.json",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the labels (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if True:
        found = _unlabelled()

        if not found:
            print("Nothing to backfill — every policy block is labelled.")
            return 0

        print(f"{len(found)} unlabelled policy block(s):\n")
        for r in found:
            graded = (f"{r['outcome']} {r['pnl_pct']:+.2f}%"
                      if r["outcome"] else "ungraded")
            print(f"  {r['ticker']:<6} {r['cycle_id']:<28} "
                  f"{r['action']}@{r['confidence']:<4} {graded:<16} "
                  f"{r['guardrail']}")

        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply.")
            return 0

        with open(UNDO_PATH, "w") as fh:
            json.dump(found, fh, indent=2, default=str)
        print(f"\nUndo snapshot written to {UNDO_PATH}")

        # `SET overridden_from = action` copies one column into another, which
        # a plain $set cannot express. Each row's own action is already in
        # `found`, so write it per id rather than reaching for a pipeline
        # update — same result, and it stays one round-trip.
        ids = [r["id"] for r in found]
        mongo_store.bulk_upsert(
            "decision_outcomes",
            [{"id": r["id"], "overridden_from": r["action"]} for r in found],
            key_field="id",
        )
        print(f"Labelled {len(ids)} row(s). action/outcome/pnl_pct untouched.")

        left = _unlabelled()
        print(f"Remaining unlabelled policy blocks: {len(left)}")
        return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
