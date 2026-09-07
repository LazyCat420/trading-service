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

    from app.services.watch_outcomes import score_completed_allocations
    result = score_completed_allocations(hours=args.hours, dry_run=args.dry_run)
    for outcome in result["outcomes"]:
        print(f"  {outcome['ticker']}: {outcome['prior_action']} -> {outcome['woke_action']} "
              f"changed={outcome['decision_changed']} risk_changed={outcome['risk_parameters_changed']}")
    print(f"scored {result['scored']}, left pending {result['pending']}"
          f"{' (dry run — nothing written)' if args.dry_run else ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
