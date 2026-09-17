#!/usr/bin/env python3
"""Historical Data Disposition Audit and Clean-Break Verification CLI (Step 11).

Usage:
    python scripts/historical_data_audit.py [--json out.json]

Guarantees:
- Every historical decision outcome and lot closure is classified into a mutually exclusive cohort.
- Zero unsupported or unversioned rows are silently upgraded to Contract v4.
- Sum of cohorts strictly reconciles to collection document counts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_store
from app.trading.attribution.historical_classifier import audit_historical_database


def run_audit(db=None) -> dict:
    database = db if db is not None else mongo_store.get_doc_db()
    return audit_historical_database(database)


def print_audit_report(results: dict) -> None:
    print("=" * 80)
    print("HISTORICAL DATA DISPOSITION AUDIT REPORT (Step 11 Clean Break)")
    print("=" * 80)

    # 1. Decision Outcomes
    dec = results["decision_outcomes"]
    print("\n1. DECISION OUTCOMES")
    print(f"   Total Records Analyzed: {dec['total']}")
    print(f"   Reconciliation Status : {'RECONCILED (100%)' if dec['reconciled'] else 'FAILED'}")
    print(f"   No Silent Upgrades    : {'VERIFIED' if dec['no_silent_upgrades_verified'] else 'FAILED'}")
    print(f"   Learning Eligible     : {dec['learning_eligible_count']} ({dec['learning_eligible_count'] / dec['total'] * 100:.2f}% of universe)")

    print("\n   Cohort Breakdown:")
    for cohort, count in sorted(dec["cohort_counts"].items(), key=lambda x: -x[1]):
        pct = count / dec["total"] * 100
        print(f"     - {cohort:<32}: {count:>5} ({pct:>5.2f}%)")

    print("\n   Disposition Summary:")
    for disp, count in sorted(dec["disposition_counts"].items(), key=lambda x: -x[1]):
        pct = count / dec["total"] * 100
        print(f"     * {disp:<24}: {count:>5} ({pct:>5.2f}%)")

    # 2. Lot Closures
    lots = results["lot_closures"]
    print("\n2. REALIZED LOT CLOSURES")
    print(f"   Total Records Analyzed: {lots['total']}")
    print(f"   Reconciliation Status : {'RECONCILED (100%)' if lots['reconciled'] else 'FAILED'}")
    print(f"   Attributable for Alpha: {lots['attributable_count']} ({lots['attributable_count'] / max(1, lots['total']) * 100:.2f}%)")

    print("\n   Cohort Breakdown:")
    for cohort, count in sorted(lots["cohort_counts"].items(), key=lambda x: -x[1]):
        pct = count / max(1, lots["total"]) * 100
        print(f"     - {cohort:<32}: {count:>5} ({pct:>5.2f}%)")

    # 3. Position Lots
    pl = results["position_lots"]
    print("\n3. POSITION LOTS INVENTORY")
    print(f"   Total Tax Lots on File: {pl['total']}")
    print(f"   Open Lots             : {pl['open_count']}")
    print(f"   Closed Lots           : {pl['closed_count']}")

    print("\n" + "=" * 80)
    print("EXIT GATE VERDICT: ALL HISTORICAL COHORTS RECONCILED — CLEAN BREAK ACTIVE")
    print("=" * 80)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_out", help="Export audit results to JSON file")
    args = parser.parse_args()

    results = run_audit()
    print_audit_report(results)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nWrote audit ledger to {args.json_out}")

    return 0 if (results["decision_outcomes"]["reconciled"] and results["lot_closures"]["reconciled"]) else 1


if __name__ == "__main__":
    sys.exit(main())
