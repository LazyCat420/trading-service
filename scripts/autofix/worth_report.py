#!/usr/bin/env python3
"""Is the autofix loop worth the energy it costs? Answer from the ledger.

The question this exists to answer is the one that killed the previous loop
late instead of early: it ran hourly for weeks, cost ~20 minutes and 47k input
tokens per proposer call, and returned 57 rejections out of 57 — and none of
that was visible in one place until someone went looking.

So the numbers are computed on evidence that accrues in MINUTES, never on P&L.
The desk's honest MDE is 8.84-11.63pp against a ~2.9pp apparent edge on 46
lifetime fills (`scripts/power_report.py`), so "did this patch improve trading"
is unanswerable for about a year and is not asked here. What is asked:

  * FUEL     - does the input feed contain anything repairable at all?
  * YIELD    - of the runs attempted, how many a human actually merged?
  * HOLDING  - did a merged fix stop the thing recurring?
  * COST     - wall clock per accepted fix.

FUEL is first on purpose. Measured 2026-08-09, it was zero: every row in
`evolution_repair_queue` had an empty traceback, and the traceback-bearing rows
in `execution_errors` were ~100% infrastructure. A loop with no fuel does not
need a better proposer, it needs a feed — and a yield of 0/0 must never read
as "working fine".

Usage:  scripts/autofix/worth_report.py [--days 14]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timedelta, timezone      # noqa: E402

from app.db import mongo_store                          # noqa: E402


def _since(days: int) -> datetime:
    """`now() - make_interval(days => N)`."""
    return datetime.now(timezone.utc) - timedelta(days=days)


def _count_if(cond: dict) -> dict:
    """`count(*) FILTER (WHERE cond)` as an aggregation accumulator."""
    return {"$sum": {"$cond": [cond, 1, 0]}}


#: `coalesce(col,'') <> ''` — true only when the field is present, non-null and
#: not the empty string. Written as an $and so a MISSING field fails it too,
#: which is what coalesce-to-empty did.
def _nonempty(field: str) -> dict:
    return {"$and": [{"$ne": [{"$ifNull": [f"${field}", ""]}, ""]}]}


def fuel(days: int) -> None:
    """What arrived that could even be attempted."""
    print("\nFUEL — is there anything to repair?")
    print("-" * 62)

    cutoff = _since(days)
    q = mongo_store.aggregate("evolution_repair_queue", [
        {"$match": {"created_at": {"$gt": cutoff}}},
        {"$group": {"_id": None, "total": {"$sum": 1},
                    "with_tb": _count_if(_nonempty("traceback_text")),
                    "with_repro": _count_if(_nonempty("repro_test")),
                    "usable": _count_if({"$or": [_nonempty("traceback_text"),
                                                 _nonempty("repro_test")]})}},
    ])
    r = q[0] if q else {}
    total = r.get("total", 0)
    with_tb = r.get("with_tb", 0)
    with_repro = r.get("with_repro", 0)
    usable = r.get("usable", 0)

    print(f"  repair queue, last {days}d ....... {total} row(s)")
    print(f"    with a traceback .............. {with_tb}")
    print(f"    with a reproduction test ...... {with_repro}")
    print(f"    ATTEMPTABLE ................... {usable}")
    if total and not usable:
        print("    ^ every row is an operational event, not a code defect.")
        print("      The watchdog reads pipeline_state.error (a message string),")
        print("      so nothing here carries a stack frame to reproduce.")

    errs = mongo_store.aggregate("execution_errors", [
        {"$match": {"created_at": {"$gt": cutoff}}},
        {"$group": {"_id": None, "total": {"$sum": 1},
                    "with_tb": _count_if(
                        {"$gt": [{"$strLenCP": {"$ifNull": ["$stack_trace", ""]}}, 50]})}},
    ])
    e = errs[0] if errs else {}
    e_total, e_tb = e.get("total", 0), e.get("with_tb", 0)
    print(f"  execution_errors, last {days}d ... {e_total} row(s), "
          f"{e_tb} with a stack trace")


def yield_(days: int) -> None:
    """What the loop produced, and what a human did with it."""
    print("\nYIELD — what came out, and what was accepted?")
    print("-" * 62)
    cutoff = _since(days)
    pushed_set = {"$ne": [{"$ifNull": ["$pushed_at", None]}, None]}
    agg = mongo_store.aggregate("autofix_runs", [
        {"$match": {"created_at": {"$gt": cutoff}}},
        {"$group": {"_id": None, "runs": {"$sum": 1},
                    "green": _count_if({"$gte": [{"$ifNull": ["$score", -1]}, 1.0]}),
                    "pushed": _count_if(pushed_set),
                    "merged": _count_if({"$eq": ["$human_verdict", "merged"]}),
                    "rejected": _count_if({"$eq": ["$human_verdict", "rejected"]}),
                    "waiting": _count_if({"$and": [
                        {"$eq": ["$human_verdict", "pending"]}, pushed_set]})}},
    ])
    a = agg[0] if agg else {}
    runs, green, pushed, merged, rejected, waiting = (
        a.get("runs", 0), a.get("green", 0), a.get("pushed", 0),
        a.get("merged", 0), a.get("rejected", 0), a.get("waiting", 0))

    print(f"  runs attempted ................. {runs}")
    print(f"  graded 1.00 .................... {green}")
    print(f"  branches pushed ................ {pushed}")
    print(f"  merged by a human .............. {merged}")
    print(f"  rejected by a human ............ {rejected}")
    print(f"  awaiting review ................ {waiting}")

    if runs == 0:
        print("\n  0 runs. This is NOT evidence the loop works or fails —")
        print("  it has not been asked to do anything. Check FUEL above.")
        return

    reviewed = merged + rejected
    if reviewed:
        print(f"\n  acceptance rate ................ {merged}/{reviewed} "
              f"({100.0 * merged / reviewed:.0f}% of reviewed)")
    else:
        print("\n  Nothing reviewed yet, so there is no acceptance rate.")

    scores = [(d["_id"], d["n"]) for d in mongo_store.aggregate("autofix_runs", [
        {"$match": {"created_at": {"$gt": cutoff}, "score": {"$nin": [None]}}},
        {"$group": {"_id": "$score", "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ])]
    if scores:
        print("\n  score ladder:")
        ladder = {0.0: "did not apply / does not compile",
                  0.25: "repro still fails",
                  0.60: "regressed, deleted a symbol, or no suite verdict",
                  1.0: "repro passes, nothing regressed"}
        for score, n in scores:
            print(f"    {float(score):.2f}  {n:>3}   {ladder.get(float(score), '')}")


def holding(days: int) -> None:
    """Did a merged fix actually stop the failure coming back?"""
    print("\nHOLDING — did merged fixes hold?")
    print("-" * 62)
    merged_runs = mongo_store.find_docs(
        "autofix_runs",
        {"human_verdict": "merged", "verdict_at": {"$gt": _since(days)}},
        sort=[("verdict_at", -1)],
    )
    # The correlated subquery, one lookup per merged fix: how many repair jobs
    # for the same target arrived AFTER the merge. `coalesce(target_symbol,'')`
    # on both sides means a null symbol matches a null symbol.
    rows = []
    for r in merged_runs:
        sym = r.get("target_symbol") or ""
        rec = mongo_store.count_docs("evolution_repair_queue", {
            "target_path": r.get("target_path"),
            "$or": [{"target_symbol": sym},
                    *([{"target_symbol": {"$in": [None, ""]}}] if sym == "" else [])],
            "created_at": {"$gt": r["verdict_at"]},
        })
        rows.append((r.get("target_path"), r.get("target_symbol"),
                     r.get("verdict_at"), rec))
    if not rows:
        print("  no merged fixes in this window — nothing to check yet.")
        return
    held = sum(1 for r in rows if not r[3])
    print(f"  merged fixes .................... {len(rows)}")
    print(f"  still quiet since merge ......... {held}")
    print(f"  recurred ........................ {len(rows) - held}")
    for path, sym, when, rec in rows:
        mark = "held" if not rec else f"RECURRED x{rec}"
        print(f"    {str(when)[:16]}  {path}::{sym}  — {mark}")


def cost(days: int) -> None:
    print("\nCOST — what did it take?")
    print("-" * 62)
    agg = mongo_store.aggregate("autofix_runs", [
        {"$match": {"created_at": {"$gt": _since(days)},
                    "wall_clock_s": {"$nin": [None]}}},
        {"$group": {"_id": None, "n": {"$sum": 1},
                    "total_s": {"$sum": "$wall_clock_s"},
                    "avg_s": {"$avg": "$wall_clock_s"},
                    "merged_s": {"$sum": {"$cond": [
                        {"$eq": ["$human_verdict", "merged"]}, "$wall_clock_s", 0]}},
                    "merged_n": _count_if({"$eq": ["$human_verdict", "merged"]})}},
    ])
    c = agg[0] if agg else {}
    n, total_s, avg_s, merged_s, merged_n = (
        c.get("n", 0), c.get("total_s", 0), c.get("avg_s", 0),
        c.get("merged_s", 0), c.get("merged_n", 0))
    if not n:
        print("  no timed runs in this window.")
        return
    print(f"  timed runs ...................... {n}")
    print(f"  total wall clock ................ {(total_s or 0) / 60:.1f} min")
    print(f"  mean per run .................... {(avg_s or 0) / 60:.1f} min")
    if merged_n:
        print(f"  per ACCEPTED fix ................ "
              f"{(total_s or 0) / 60 / merged_n:.1f} min "
              f"(all runs / {merged_n} merged)")
    else:
        print("  per accepted fix ................ undefined — 0 merged")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=14)
    args = p.parse_args()

    print("=" * 62)
    print(f"  AUTOFIX WORTH REPORT — last {args.days} days")
    print("=" * 62)
    fuel(args.days)
    yield_(args.days)
    holding(args.days)
    cost(args.days)
    print("\n" + "=" * 62)
    print("  No P&L verdict appears here on purpose: the desk's MDE is")
    print("  8.84-11.63pp on 46 lifetime fills, so patch-level trading")
    print("  impact is unmeasurable for ~a year. See power_report.py.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
