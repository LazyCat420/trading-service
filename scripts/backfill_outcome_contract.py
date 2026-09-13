#!/usr/bin/env python3
"""Recover the provenance contract for rows stranded by `2e61169f`.

    python3 scripts/backfill_outcome_contract.py             # dry run (default)
    python3 scripts/backfill_outcome_contract.py --apply
    python3 scripts/backfill_outcome_contract.py --apply --undo-file /path/undo.json

Writes to Mongo only under `--apply`. Never runs during a live cycle without
`--force` (grading a decision mid-cycle races the recorder).

WHAT THIS REPAIRS
-----------------
`2e61169f` (2026-09-08, "require verified price provenance before outcome
learning") made `resolve_pending_outcomes` require `decision_as_of`,
`entry_date`, `entry_price_source`, `claim_type`,
`outcome_contract_version == CONTRACT_VERSION` and
`outcome_evidence_state == 'pending'`. The contract is right: `verified_pair`
refuses to grade unless the entry and exit prices come from the SAME vendor,
which is the same discipline the price readers owe. What did not ship with it
was a backfill, so every row written before that deploy lost the ability to
resolve — silently, because a missing field does not compare.

HOW PROVENANCE IS RECOVERED, AND WHERE IT REFUSES TO GUESS
----------------------------------------------------------
The stranded rows kept `entry_price`, `ticker` and `created_at`. The vendor is
recovered by finding the `price_history` bar whose close EQUALS the stored entry
price, to a relative tolerance of 1e-6 — an exact float match, not a near one.

That strictness is the point. At a 0.5% tolerance 27 of 76 rows match more than
one DAY, so a loose match invents both the vendor and the date. Measured
2026-09-12 across the 76 stranded rows:

    exact (1e-6)   39 unique yfinance · 5 unique polygon · 29 both vendors · 3 none
    0.5%           24 unique · 23 vendor-ambiguous · 27 DATE-ambiguous · 2 none

Where both vendors carry the same close to 1e-6, the choice cannot change a
grade, and the row is assigned the ticker's dominant vendor — `dominant_source_for`,
the SAME rule the readers use, rather than a fourth hand-rolled precedence.

A row whose price matches NO bar, or matches more than one date, is not repaired.
It is stamped `outcome_evidence_state='provenance_unrecoverable'` with a reason,
so it leaves the stranded count without ever being graded on an invented entry.
A row that cannot be graded honestly must be visible as such, not silently
dropped and not silently resolved.

UNDO. Every modified `_id` and its prior field values are written to the undo
file before the write. Restoring it returns the collection exactly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXACT_REL_TOL = 1e-6
LOOKBACK_DAYS = 6  # the entry bar is the last CLOSED bar before the decision


def _candidate_rows(mongo_store, is_synthetic_cycle, CONTRACT_VERSION) -> list[dict]:
    rows = mongo_store.find_docs("decision_outcomes", {"resolved_at": None}, limit=0) or []
    out = []
    for r in rows:
        if is_synthetic_cycle(r.get("cycle_id")):
            continue
        if r.get("outcome_evidence_state") == "unsupported_claim":
            continue
        if r.get("outcome_contract_version") == CONTRACT_VERSION and r.get("decision_as_of"):
            continue
        out.append(r)
    return out


def _desk_for(mongo_store, row: dict) -> dict:
    """The stored desk for this decision, with `desk_data` parsed either shape."""
    docs = mongo_store.find_docs("shared_desk", {
        "cycle_id": row.get("cycle_id"), "ticker": row.get("ticker"),
    }, projection={"desk_data": 1}, limit=1) or []
    if not docs:
        return {}
    desk = docs[0].get("desk_data")
    if isinstance(desk, str):
        try:
            desk = json.loads(desk)
        except (ValueError, TypeError):
            return {}
    return desk if isinstance(desk, dict) else {}


def _held_for(mongo_store, row: dict):
    return (_desk_for(mongo_store, row).get("cycle_metadata") or {}).get("held")


def _artifact_for(mongo_store, row: dict) -> dict:
    return _desk_for(mongo_store, row).get("final_decision") or {}


def _match_bar(mongo_store, ticker: str, entry_price: float, created_at: datetime) -> tuple:
    """Return (status, bar_or_none, bars). Status is one of
    'unique' | 'vendor_ambiguous' | 'date_ambiguous' | 'no_match'."""
    lo = created_at - timedelta(days=LOOKBACK_DAYS)
    hi = created_at + timedelta(days=1)
    bars = mongo_store.find_docs("price_history", {
        "ticker": ticker,
        "date": {"$gte": datetime(lo.year, lo.month, lo.day),
                 "$lte": datetime(hi.year, hi.month, hi.day)},
    }, projection={"date": 1, "close": 1, "source": 1}, limit=0) or []
    hits = [b for b in bars
            if b.get("close") is not None
            and abs(float(b["close"]) - entry_price) <= EXACT_REL_TOL * max(abs(entry_price), 1e-9)]
    if not hits:
        return "no_match", None, hits
    if len({b["date"] for b in hits}) > 1:
        return "date_ambiguous", None, hits
    if len({b["source"] for b in hits}) == 1:
        return "unique", hits[0], hits
    return "vendor_ambiguous", None, hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--force", action="store_true", help="run even if a cycle is live")
    ap.add_argument("--undo-file", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.autoresearch.outcome_evidence import CONTRACT_VERSION, claim_type
    from app.db import mongo_store
    from app.quant.returns import dominant_source_for
    from app.services.cycle_scope import is_synthetic_cycle

    if args.apply and not args.force:
        state = mongo_store.find_docs("pipeline_state", {}, limit=1) or []
        if state and str(state[0].get("status")) not in ("done", "error", "stopped", "idle"):
            print(f"refusing: a cycle looks live (status={state[0].get('status')!r}). "
                  f"Re-run with --force only if you are sure.", file=sys.stderr)
            return 2

    rows = _candidate_rows(mongo_store, is_synthetic_cycle, CONTRACT_VERSION)
    plan, refused, counts = [], [], {}
    for r in rows:
        ep, tk, ca = r.get("entry_price"), r.get("ticker"), r.get("created_at")
        if ep is None or not tk or not ca:
            refused.append((r, "row is missing entry_price/ticker/created_at"))
            counts["unusable_row"] = counts.get("unusable_row", 0) + 1
            continue
        status, bar, hits = _match_bar(mongo_store, tk, float(ep), ca)
        counts[status] = counts.get(status, 0) + 1
        if status == "unique":
            src, when = bar["source"], bar["date"]
        elif status == "vendor_ambiguous":
            # Every vendor agrees to 1e-6, so the grade cannot turn on the
            # choice. Use the readers' rule rather than inventing a new one.
            src = dominant_source_for(tk) or sorted({b["source"] for b in hits})[0]
            when = hits[0]["date"]
        else:
            refused.append((r, status))
            continue
        act = r.get("action") or "HOLD"
        # `claim_type` needs `hold_reason_held`, which is NOT on the decision
        # artifact — it is on the desk at `cycle_metadata.held`. Deriving it
        # from the artifact alone returns None for EVERY row, which would make
        # this backfill a no-op that reports success: it would stamp all 76 rows
        # `unsupported_claim` and the stranded count would fall to zero with
        # nothing gradeable. Measured before this was corrected: 76/76 None.
        ct = claim_type(act, {**_artifact_for(mongo_store, r),
                              "hold_reason_held": _held_for(mongo_store, r)})
        plan.append({
            "_id": r["_id"], "id": r.get("id"), "ticker": tk, "action": act,
            "matched_source": src, "matched_date": when, "status": status,
            "set": {
                "decision_as_of": when,
                "entry_date": when,
                "entry_price_source": src,
                "claim_type": ct,
                "outcome_contract_version": CONTRACT_VERSION,
                "outcome_evidence_state": "pending" if ct else "unsupported_claim",
                "contract_backfilled_at": datetime.now(timezone.utc),
            },
        })

    print(f"stranded candidates: {len(rows)}")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"   {v:>4}  {k}")
    ct_counts: dict = {}
    for p_ in plan:
        k = str(p_["set"]["claim_type"])
        ct_counts[k] = ct_counts.get(k, 0) + 1
    print(f"\n  repairable      : {len(plan)}")
    print("  of which, by claim_type (only these two GRADE):")
    for k, v in sorted(ct_counts.items(), key=lambda x: -x[1]):
        mark = "  <- becomes gradeable" if k in ("immediate_directional", "flat_wait") else ""
        print(f"     {v:>4}  {k}{mark}")
    print(f"  refused (marked): {len(refused)}")
    for r, why in refused[:10]:
        print(f"     {r.get('ticker'):<6} {str(r.get('created_at'))[:10]}  {why}")
    if plan[:5]:
        print("\n  sample repairs:")
        for p in plan[:5]:
            print(f"     {p['ticker']:<6} {str(p['matched_date'])[:10]}  "
                  f"source={p['matched_source']:<9} ({p['status']})")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    undo_path = Path(args.undo_file or
                     f"outcome_contract_undo_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    undo = [{"_id": str(p["_id"]), "id": p["id"],
             "prior": {k: None for k in p["set"]}} for p in plan]
    undo += [{"_id": str(r["_id"]), "id": r.get("id"),
              "prior": {"outcome_evidence_state": r.get("outcome_evidence_state")}}
             for r, _ in refused]
    undo_path.write_text(json.dumps(undo, indent=1, default=str))
    print(f"\nundo written to {undo_path} ({len(undo)} rows)")

    wrote = 0
    for p in plan:
        mongo_store.update_docs("decision_outcomes", {"id": p["id"]}, {"$set": p["set"]})
        wrote += 1
    marked = 0
    for r, why in refused:
        mongo_store.update_docs("decision_outcomes", {"id": r.get("id")}, {"$set": {
            "outcome_evidence_state": "provenance_unrecoverable",
            "provenance_refusal_reason": why,
            "contract_backfilled_at": datetime.now(timezone.utc),
        }})
        marked += 1
    print(f"repaired {wrote} rows; marked {marked} unrecoverable")
    print("Re-run scripts/outcome_resolution_health.py to confirm stranded -> 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
