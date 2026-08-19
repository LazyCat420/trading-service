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

from scripts.migration.pg_connection import get_db  # noqa: E402

SELECT = """
SELECT d.id, d.cycle_id, d.ticker, d.action, d.confidence,
       d.overridden_from, d.outcome, d.pnl_pct, g.guardrail
FROM decision_outcomes d
JOIN v3_guardrail_firings g
  ON g.cycle_id = d.cycle_id AND g.ticker = d.ticker
WHERE g.guardrail LIKE 'HOLD_POLICY_BLOCKED%%'
  AND d.overridden_from IS NULL
ORDER BY d.created_at
"""

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

    with get_db() as db:
        rows = db.execute(SELECT).fetchall()
        cols = ["id", "cycle_id", "ticker", "action", "confidence",
                "overridden_from", "outcome", "pnl_pct", "guardrail"]
        found = [dict(zip(cols, r)) for r in rows]

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

        ids = [r["id"] for r in found]
        db.execute(
            "UPDATE decision_outcomes SET overridden_from = action "
            "WHERE id = ANY(%s)",
            [ids],
        )
        print(f"Labelled {len(ids)} row(s). action/outcome/pnl_pct untouched.")

        left = db.execute(SELECT).fetchall()
        print(f"Remaining unlabelled policy blocks: {len(left)}")
        return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
