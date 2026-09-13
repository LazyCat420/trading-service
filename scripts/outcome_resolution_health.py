#!/usr/bin/env python3
"""Can the desk still learn? — the resolvable population of `decision_outcomes`.

    python3 scripts/outcome_resolution_health.py
    python3 scripts/outcome_resolution_health.py --json
    python3 scripts/outcome_resolution_health.py --max-silent-days 3   # exit 1 past that

Read-only. Safe against a live cycle. Reads Mongo.

WHY THIS EXISTS
---------------
On 2026-09-08, `2e61169f` ("require verified price provenance before outcome
learning") tightened `resolve_pending_outcomes` to require
`outcome_contract_version`, `outcome_evidence_state == 'pending'` and a
`decision_as_of` newer than the row's horizon. Every row written before that
deploy lacks all three — **2,783 of 2,801** — and a missing field does not
compare, so `{'decision_as_of': {'$lt': cutoff}}` can never match one.

The contract is right. What was missing is this file. The resolver kept running,
kept finding nothing, and reported nothing, so the desk stopped learning on
2026-09-04 and no number anywhere moved. Every downstream reader — scorecards,
challenger grading, decision quality, the memory writeback — went on quoting a
ledger that had stopped.

That is the shape this report exists to make loud:

  * **a filter that matches zero rows is indistinguishable from a quiet system**
    unless something states the population it was drawn from, and
  * **a contract tightened on the reader strands the writer's whole history**
    unless a backfill ships in the same change.

WHAT A ZERO MEANS HERE. `due_and_eligible: 0` is only good news when
`stranded: 0` too. A zero beside a non-zero stranded count is the failure above
recurring, and this report says so in words rather than printing a clean 0.

AND `unsupported_claim` IS NOT AUTOMATICALLY "BY DESIGN". The contract grades
only `immediate_directional` (a BUY/SELL entered now) and `flat_wait` (a HOLD on
a name NOT already held). Everything else is legitimately unsupported — but if
almost EVERYTHING is unsupported, the contract is not filtering, it is starving.
The first draft of this report filed that category under "by design" and would
have printed a clean bill of health over exactly the defect that motivated it:

    measured 2026-09-12, before the recorder fix
    decisions in 90 days ......... 1,557
    with a claim_type ............     7   (0.4%)

So the GRADEABLE RATE is reported first, and a rate under `--min-gradeable-pct`
is an error. A filter that admits nothing looks identical to a clean system
unless something states the population it started from.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone


def _load() -> dict:
    from app.autoresearch.outcome_evidence import CONTRACT_VERSION
    from app.autoresearch.outcome_tracker import RESOLVE_AFTER_DAYS
    from app.db import mongo_store
    from app.services.cycle_scope import exclude_synthetic, is_synthetic_cycle

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RESOLVE_AFTER_DAYS)

    # The resolver's own filter, verbatim. If this drifts from
    # `resolve_pending_outcomes`, this report is measuring a query nobody runs —
    # so it is imported-by-shape, not restated: the constants come from the
    # modules that define them.
    eligible = mongo_store.find_docs("decision_outcomes", {
        "resolved_at": None,
        "decision_as_of": {"$lt": cutoff},
        "outcome_contract_version": CONTRACT_VERSION,
        "outcome_evidence_state": "pending",
        **exclude_synthetic(),
    }, limit=0) or []

    unresolved = mongo_store.find_docs(
        "decision_outcomes", {"resolved_at": None}, limit=0) or []

    stranded, by_design_syn, by_design_claim, not_yet_due = [], [], [], []
    for row in unresolved:
        if is_synthetic_cycle(row.get("cycle_id")):
            by_design_syn.append(row)
        elif row.get("outcome_evidence_state") == "unsupported_claim":
            by_design_claim.append(row)
        elif row.get("decision_as_of") is None or row.get(
                "outcome_contract_version") != CONTRACT_VERSION:
            stranded.append(row)
        else:
            # Carries the contract and a real decision_as_of. Whether it is DUE
            # is the horizon test; `eligible` above already holds the due ones,
            # so this branch only needs to separate the not-yet-due.
            as_of = row["decision_as_of"]
            as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
            if as_of >= cutoff:
                not_yet_due.append(row)

    newest = mongo_store.find_docs(
        "decision_outcomes", {"resolved_at": {"$ne": None}},
        sort=[("resolved_at", -1)], limit=1) or []
    last_resolved = newest[0]["resolved_at"] if newest else None
    if last_resolved is not None and last_resolved.tzinfo is None:
        last_resolved = last_resolved.replace(tzinfo=timezone.utc)
    silent_days = (now - last_resolved).total_seconds() / 86400 if last_resolved else None

    total = mongo_store.count_docs("decision_outcomes", {})
    with_contract = mongo_store.count_docs(
        "decision_outcomes", {"outcome_contract_version": CONTRACT_VERSION})

    # The headline. Of the rows written UNDER the contract, how many carry a
    # claim the contract can actually grade? A high unsupported share means the
    # desk's output and the contract's vocabulary do not meet.
    graded_pop = mongo_store.find_docs(
        "decision_outcomes", {"outcome_contract_version": CONTRACT_VERSION},
        projection={"claim_type": 1, "action": 1}, limit=0) or []
    by_claim: dict[str, int] = {}
    for r in graded_pop:
        by_claim[str(r.get("claim_type"))] = by_claim.get(str(r.get("claim_type")), 0) + 1
    gradeable = sum(v for k, v in by_claim.items() if k in ("immediate_directional", "flat_wait"))
    pct = (100.0 * gradeable / len(graded_pop)) if graded_pop else None

    return {
        "gradeable_rows": gradeable,
        "contract_population": len(graded_pop),
        "gradeable_pct": round(pct, 1) if pct is not None else None,
        "by_claim_type": by_claim,
        "generated_at": now.isoformat(),
        "horizon_days": RESOLVE_AFTER_DAYS,
        "contract_version": CONTRACT_VERSION,
        "rows_total": total,
        "rows_carrying_the_contract": with_contract,
        "rows_predating_the_contract": total - with_contract,
        "unresolved_total": len(unresolved),
        "due_and_eligible": len(eligible),
        "stranded_no_contract": len(stranded),
        "excluded_synthetic": len(by_design_syn),
        "excluded_unsupported_claim": len(by_design_claim),
        "pending_not_yet_due": len(not_yet_due),
        "last_resolved_at": last_resolved.isoformat() if last_resolved else None,
        "days_since_last_resolution": round(silent_days, 2) if silent_days is not None else None,
        "stranded_sample": [
            {"id": r.get("id"), "cycle_id": r.get("cycle_id"),
             "ticker": r.get("ticker"), "action": r.get("action"),
             "created_at": str(r.get("created_at"))[:19]}
            for r in stranded[:10]
        ],
    }


def _render(rep: dict, max_silent: float | None, min_gradeable: float | None) -> int:
    print("decision_outcomes — can the desk still learn?")
    print(f"  generated            {rep['generated_at'][:19]}")
    print(f"  horizon              {rep['horizon_days']}d, contract v{rep['contract_version']}")
    print()
    print("  GRADEABLE RATE (of rows written under the contract)")
    print(f"    gradeable          {rep['gradeable_rows']}/{rep['contract_population']}"
          f"  = {rep['gradeable_pct']}%")
    for k, v in sorted(rep["by_claim_type"].items(), key=lambda x: -x[1]):
        mark = "  <- gradeable" if k in ("immediate_directional", "flat_wait") else ""
        print(f"      claim_type={k:<22} {v}{mark}")
    print()
    print(f"  rows total           {rep['rows_total']}")
    print(f"    carrying contract  {rep['rows_carrying_the_contract']}")
    print(f"    predating contract {rep['rows_predating_the_contract']}")
    print()
    print(f"  unresolved           {rep['unresolved_total']}")
    print(f"    due and eligible   {rep['due_and_eligible']}   <- what the resolver can act on")
    print(f"    pending, not due   {rep['pending_not_yet_due']}")
    print(f"    STRANDED           {rep['stranded_no_contract']}   <- real cycles the contract locked out")
    print(f"    excluded: synthetic          {rep['excluded_synthetic']}  (by design)")
    print(f"    excluded: unsupported_claim  {rep['excluded_unsupported_claim']}  (by design)")
    print()
    print(f"  last resolution      {rep['last_resolved_at'] or 'NEVER'}"
          f"  ({rep['days_since_last_resolution']}d ago)"
          if rep["last_resolved_at"] else "  last resolution      NEVER")

    rc = 0
    if rep["stranded_no_contract"]:
        print()
        print(f"  !! {rep['stranded_no_contract']} rows are from REAL cycles and can never resolve:")
        print("     they predate the provenance contract, and a missing field does not compare.")
        print("     Repair with: python3 scripts/backfill_outcome_contract.py --apply")
        for s in rep["stranded_sample"]:
            print(f"       {s['created_at']}  {s['ticker']:<6} {s['action']:<5} {s['cycle_id']}")
        rc = 1
    if max_silent is not None and rep["days_since_last_resolution"] is not None \
            and rep["days_since_last_resolution"] > max_silent:
        print()
        print(f"  !! nothing has resolved for {rep['days_since_last_resolution']}d "
              f"(--max-silent-days {max_silent})")
        rc = 1
    if min_gradeable is not None and rep["gradeable_pct"] is not None \
            and rep["gradeable_pct"] < min_gradeable:
        print()
        print(f"  !! only {rep['gradeable_pct']}% of contract rows carry a gradeable claim "
              f"(--min-gradeable-pct {min_gradeable}).")
        print("     The contract is not filtering, it is starving: the desk's output and")
        print("     the contract's vocabulary do not meet. Check that the recorder passes")
        print("     `hold_reason_held` (from the desk's cycle_metadata.held) into claim_type.")
        rc = 1
    if rc == 0:
        print()
        print("  OK — the resolver has a population to work on and is working through it.")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable")
    ap.add_argument("--max-silent-days", type=float, default=None,
                    help="exit 1 if nothing has resolved in this many days")
    ap.add_argument("--min-gradeable-pct", type=float, default=None,
                    help="exit 1 if fewer than this %% of contract rows are gradeable")
    args = ap.parse_args()

    rep = _load()
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
        return 1 if rep["stranded_no_contract"] else 0
    return _render(rep, args.max_silent_days, args.min_gradeable_pct)


if __name__ == "__main__":
    sys.exit(main())
