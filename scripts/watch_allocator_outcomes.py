#!/usr/bin/env python3
"""Score what each fired allocation actually bought. Fills `watch_triage_log.outcome`.

A shadow log that records only what the allocator WOULD do cannot be graded.
This is the other half: for every row whose wake actually fired, join to the
cycle it started and record whether a decision came out and whether it differed
from the one that was standing.

TWO THINGS IT REFUSES TO DO
---------------------------
1. It never writes an outcome for a cycle that has not finished. A row scored
   mid-cycle would record "no decision produced" for work still in progress —
   the field would then read as a measurement of failure rather than of
   pending, and every rate computed from it would be wrong in the same
   direction.
2. It never calls a DEGRADED prior a decision. 10 of the 30 captured trips had
   one, so a naive `prior != woke` comparison scores a third of the window as
   "the decision changed" when what actually happened is that a failure was
   replaced by a decision.

Usage:
    python scripts/watch_allocator_outcomes.py [--hours 168] [--dry-run]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REAL_ACTIONS = {"BUY", "SELL", "HOLD"}
TERMINAL = {"done", "error", "stopped", "interrupted", "completed"}


def _action(row) -> str | None:
    if not row:
        return None
    rj = row.get("result_json")
    try:
        rj = json.loads(rj) if isinstance(rj, str) else (rj or {})
    except (ValueError, TypeError):
        return None
    return ((rj.get("action") or "").upper()) or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app.db.mongo import get_mongo_client
    from app.db.mongo_store import TRADING_MONGO_DB

    db = get_mongo_client()[TRADING_MONGO_DB]
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    pending = list(db.watch_triage_log.find(
        {"created_at": {"$gte": since}, "fired": True, "outcome": None}))
    print(f"{len(pending)} fired allocation(s) awaiting an outcome")

    scored = skipped = 0
    for row in pending:
        cmd = db.v3_system_commands.find_one({"id": row.get("cycle_id")})
        cycle = None
        if cmd and cmd.get("result"):
            try:
                parsed = (ast.literal_eval(cmd["result"])
                          if isinstance(cmd["result"], str) else cmd["result"])
                cycle = (parsed or {}).get("cycle_id")
            except (ValueError, SyntaxError, TypeError, AttributeError):
                cycle = None
        if not cycle:
            skipped += 1
            continue

        summary = db.cycle_run_summaries.find_one({"cycle_id": cycle}) or {}
        status = (summary.get("status") or "").lower()
        if status not in TERMINAL:
            # Still running. Leaving `outcome` absent is the point — see the
            # module docstring.
            skipped += 1
            continue

        woke = db.analysis_results.find_one({"cycle_id": cycle, "ticker": row["ticker"]})
        prior = db.analysis_results.find_one(
            {"ticker": row["ticker"], "created_at": {"$lt": row["created_at"]}},
            sort=[("created_at", -1)])
        prior_action, woke_action = _action(prior), _action(woke)

        if prior_action not in REAL_ACTIONS:
            # Not comparable. `None` here, and a reason, so a consumer cannot
            # mistake it for "unchanged".
            changed = None
            note = "prior_was_not_a_decision"
        elif woke_action is None:
            changed = None
            note = "no_decision_produced"
        else:
            changed = woke_action != prior_action
            note = "comparable"

        outcome = {
            "cycle_id": cycle, "cycle_status": status,
            "prior_action": prior_action, "woke_action": woke_action,
            "decision_changed": changed, "comparability": note,
            "scored_at": datetime.now(timezone.utc),
        }
        print(f"  {row['ticker']:6} {row['id']}  {prior_action} -> {woke_action}  "
              f"changed={changed}  ({note})")
        if not args.dry_run:
            db.watch_triage_log.update_one({"id": row["id"]},
                                           {"$set": {"outcome": outcome}})
        scored += 1

    print(f"\nscored {scored}, left pending {skipped}"
          f"{' (dry run — nothing written)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
