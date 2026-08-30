#!/usr/bin/env python3
"""Backfill `final_decision` onto desks that lost it, from `trade_results`.

Between 2026-07-06 and 2026-07-25, ~2% of desks reached PM_DONE with a real
decision saved to `trade_results` while `shared_desk.desk_data->'final_decision'`
stayed null. Cause: `final_decision` only propagated when the board returned
SUCCESS/DATA_GAP; any other degrade path wrote nothing, and the pipeline fell
through to a hardcoded HOLD@0. Fixed forward in `orchestrator.py` (an explicit
degraded sentinel is now always written).

This repairs the history so desk-derived counts stop under-counting — every
known case is a HOLD, so the omission is NOT random and skews any distribution
computed from desks.

Backfilled artifacts are stamped so they can never be mistaken for natively
written ones:

    decision_provenance : "board_degraded_fallback"
    _backfilled_from    : "trade_results"
    _backfilled_at      : <iso timestamp>

    python scripts/backfill_desk_decisions.py            # dry run (default)
    python scripts/backfill_desk_decisions.py --apply    # write

ALWAYS back up first — see HANDOFF. A gzipped JSONL dump of `shared_desk` +
`trade_results` was taken before the first run of this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default: dry run)")
    ap.add_argument("--since", default="2026-07-01")
    args = ap.parse_args()

    from app.db import mongo_store

    # INNER JOIN on (cycle_id, ticker). mongo_query.join_rows deliberately
    # supports ONE equality key, so a two-column join is stitched here — and
    # stitched the same way join_rows does it (index the right side, emit only
    # matched left rows) rather than with $lookup, whose left-outer semantics
    # would turn this filter into a pass-through.
    trades = {}
    for t in mongo_store.find_docs("trade_results", {}):
        trades[(t.get("cycle_id"), t.get("ticker"))] = t

    rows = []
    for d in mongo_store.find_docs("shared_desk", {"created_at": {"$gt": args.since}}):
        t = trades.get((d.get("cycle_id"), d.get("ticker")))
        if t is None:
            continue  # no match, no row — INNER JOIN
        rows.append((d.get("desk_id"), d.get("cycle_id"), d.get("ticker"),
                     d.get("desk_data"), t.get("action"), t.get("confidence"),
                     t.get("position_size_pct"), t.get("reasoning")))

    repairs = []
    for desk_id, cycle_id, ticker, desk_data, action, conf, size, reasoning in rows:
        data = desk_data if isinstance(desk_data, dict) else json.loads(desk_data)
        fd = (data or {}).get("final_decision")
        if isinstance(fd, dict) and fd.get("action"):
            continue  # healthy
        if not action:
            continue  # nothing to backfill from
        repairs.append({
            "desk_id": desk_id, "cycle_id": cycle_id, "ticker": ticker,
            "action": action, "confidence": conf,
            "position_size_pct": size, "reasoning": reasoning,
        })

    print(f"desks joined to trade_results since {args.since}: {len(rows)}")
    print(f"desks MISSING final_decision but with a saved decision: {len(repairs)}")
    for r in repairs:
        print(f"   {r['ticker']:6s} {str(r['action']):5s} @{r['confidence']}  "
              f"{r['cycle_id'][-14:]}")

    if not repairs:
        print("\nnothing to repair.")
        return 0

    if not args.apply:
        print("\nDRY RUN — re-run with --apply to write.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    written = 0
    if True:
        for r in repairs:
            artifact = {
                "summary": (
                    "Backfilled from trade_results: the board's decision was "
                    "produced and executed but never persisted to the desk "
                    "(pre-2026-07-25 write-path bug)."
                ),
                "action": r["action"],
                "confidence": r["confidence"],
                "position_size_pct": r["position_size_pct"],
                "reasoning": r["reasoning"],
                "decision_provenance": "board_degraded_fallback",
                "_backfilled_from": "trade_results",
                "_backfilled_at": now,
                "_artifact_type": "final_decision",
                "risk_flags": ["backfilled_decision"],
            }
            # jsonb_set(desk_data, '{final_decision}', ...) has NO Mongo
            # equivalent here, because `desk_data` is stored as JSON *TEXT*,
            # not as a sub-document. `$set: {"desk_data.final_decision": ...}`
            # would silently create a dotted field beside the string and leave
            # the real artifact untouched. Read, patch, re-serialise.
            doc = mongo_store.find_docs(
                "shared_desk", {"desk_id": r["desk_id"]}, limit=1)
            if not doc:
                continue
            raw = doc[0].get("desk_data")
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            data["final_decision"] = artifact
            mongo_store.update_docs(
                "shared_desk", {"desk_id": r["desk_id"]},
                {"$set": {
                    # written back in the SAME shape it was read in, so this
                    # script cannot be the thing that changes desk_data's type
                    "desk_data": (json.dumps(data, default=str)
                                  if isinstance(raw, str) else data),
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
            written += 1

    print(f"\nbackfilled {written} desk(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
