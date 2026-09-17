#!/usr/bin/env python3
"""Audit or recover legacy outcome semantics from immutable SharedDesk evidence.

Dry-run is the default. ``--apply`` only adds provenance fields or upgrades a
legacy row to an already-supported pending claim; it never resolves prices,
changes an action, or treats a conditional entry as an immediate trade.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.autoresearch.outcome_recovery import classify
from app.db import mongo_store


def _desk(cycle_id: str, ticker: str):
    rows = mongo_store.find_docs("shared_desk", {"cycle_id": cycle_id, "ticker": ticker},
                                 projection={"desk_data": 1}, limit=1)
    return rows[0].get("desk_data") if rows else None


def recover(*, cycle_prefix: str | None = None, skill_version: int | None = None,
            apply: bool = False) -> dict:
    query = {"outcome_contract_version": 2}
    if cycle_prefix:
        query["cycle_id"] = {"$regex": "^" + cycle_prefix}
    if skill_version is not None:
        # The board is the final decision owner.  Its delivered version is the
        # cohort key for a reviewed-method audit; do not pool versions.
        query["skill_versions.v3_board_of_directors"] = int(skill_version)
    rows = mongo_store.find_docs("decision_outcomes", query, sort=[("created_at", 1)])
    proposals = [classify(row, _desk(str(row.get("cycle_id") or ""), str(row.get("ticker") or "")))
                 for row in rows]
    if apply:
        for proposal in proposals:
            update = {"evaluation_type": proposal.evaluation_type,
                      "recovery": proposal.to_dict()}
            # Only these two types already have a mathematically defined,
            # source-pinned resolver. Conditional and existing-position rows
            # remain pending evidence, never silently promoted into scorecards.
            if proposal.claim_type:
                update["claim_type"] = proposal.claim_type
                update["outcome_evidence_state"] = proposal.evidence_state
            mongo_store.update_docs("decision_outcomes", {"id": proposal.outcome_id}, {"$set": update})
    counts = Counter(p.evaluation_type for p in proposals)
    return {"source_rows": len(rows), "dispositions": dict(sorted(counts.items())),
            "applied": apply, "proposals": [p.to_dict() for p in proposals]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle-prefix", default="cycle-v3-", help="limit audit to a cycle-id prefix")
    parser.add_argument("--skill-version", type=int,
                        help="limit to a board skill version; use the reviewed baseline for admission")
    parser.add_argument("--apply", action="store_true", help="persist only provenance and supported claim upgrades")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = recover(cycle_prefix=args.cycle_prefix, skill_version=args.skill_version,
                     apply=args.apply)
    print(json.dumps(report, indent=2, default=str))
    # A reconciliation failure is visible to automation, not buried in prose.
    return 0 if report["source_rows"] == sum(report["dispositions"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
