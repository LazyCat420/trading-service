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

from scripts.migration.pg_connection import get_db          # noqa: E402


def _rows(db, sql: str, params: list) -> list:
    return db.execute(sql, params).fetchall() or []


def fuel(db, days: int) -> None:
    """What arrived that could even be attempted."""
    print("\nFUEL — is there anything to repair?")
    print("-" * 62)

    q = _rows(db, """
        SELECT count(*) total,
               count(*) FILTER (WHERE coalesce(traceback_text,'') <> '') with_tb,
               count(*) FILTER (WHERE coalesce(repro_test,'') <> '')     with_repro
        FROM evolution_repair_queue
        WHERE created_at > now() - make_interval(days => %s)
    """, [days])
    total, with_tb, with_repro = (q[0] if q else (0, 0, 0))
    usable = _rows(db, """
        SELECT count(*) FROM evolution_repair_queue
        WHERE created_at > now() - make_interval(days => %s)
          AND (coalesce(traceback_text,'') <> '' OR coalesce(repro_test,'') <> '')
    """, [days])[0][0]

    print(f"  repair queue, last {days}d ....... {total} row(s)")
    print(f"    with a traceback .............. {with_tb}")
    print(f"    with a reproduction test ...... {with_repro}")
    print(f"    ATTEMPTABLE ................... {usable}")
    if total and not usable:
        print("    ^ every row is an operational event, not a code defect.")
        print("      The watchdog reads pipeline_state.error (a message string),")
        print("      so nothing here carries a stack frame to reproduce.")

    errs = _rows(db, """
        SELECT count(*) total,
               count(*) FILTER (WHERE stack_trace IS NOT NULL
                                 AND length(stack_trace) > 50) with_tb
        FROM execution_errors
        WHERE created_at > now() - make_interval(days => %s)
    """, [days])
    e_total, e_tb = (errs[0] if errs else (0, 0))
    print(f"  execution_errors, last {days}d ... {e_total} row(s), "
          f"{e_tb} with a stack trace")


def yield_(db, days: int) -> None:
    """What the loop produced, and what a human did with it."""
    print("\nYIELD — what came out, and what was accepted?")
    print("-" * 62)
    rows = _rows(db, """
        SELECT count(*) runs,
               count(*) FILTER (WHERE score >= 1.0)                green,
               count(*) FILTER (WHERE pushed_at IS NOT NULL)       pushed,
               count(*) FILTER (WHERE human_verdict = 'merged')    merged,
               count(*) FILTER (WHERE human_verdict = 'rejected')  rejected,
               count(*) FILTER (WHERE human_verdict = 'pending'
                                  AND pushed_at IS NOT NULL)       waiting
        FROM autofix_runs
        WHERE created_at > now() - make_interval(days => %s)
    """, [days])
    runs, green, pushed, merged, rejected, waiting = (
        rows[0] if rows else (0, 0, 0, 0, 0, 0))

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

    scores = _rows(db, """
        SELECT score, count(*) FROM autofix_runs
        WHERE created_at > now() - make_interval(days => %s) AND score IS NOT NULL
        GROUP BY score ORDER BY score
    """, [days])
    if scores:
        print("\n  score ladder:")
        ladder = {0.0: "did not apply / does not compile",
                  0.25: "repro still fails",
                  0.60: "regressed, deleted a symbol, or no suite verdict",
                  1.0: "repro passes, nothing regressed"}
        for score, n in scores:
            print(f"    {float(score):.2f}  {n:>3}   {ladder.get(float(score), '')}")


def holding(db, days: int) -> None:
    """Did a merged fix actually stop the failure coming back?"""
    print("\nHOLDING — did merged fixes hold?")
    print("-" * 62)
    rows = _rows(db, """
        SELECT r.target_path, r.target_symbol, r.verdict_at,
               (SELECT count(*) FROM evolution_repair_queue q
                 WHERE q.target_path = r.target_path
                   AND coalesce(q.target_symbol,'') = coalesce(r.target_symbol,'')
                   AND q.created_at > r.verdict_at) AS recurrences
        FROM autofix_runs r
        WHERE r.human_verdict = 'merged'
          AND r.verdict_at IS NOT NULL
          AND r.verdict_at > now() - make_interval(days => %s)
        ORDER BY r.verdict_at DESC
    """, [days])
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


def cost(db, days: int) -> None:
    print("\nCOST — what did it take?")
    print("-" * 62)
    rows = _rows(db, """
        SELECT count(*), sum(wall_clock_s), avg(wall_clock_s),
               sum(wall_clock_s) FILTER (WHERE human_verdict = 'merged'),
               count(*) FILTER (WHERE human_verdict = 'merged')
        FROM autofix_runs
        WHERE created_at > now() - make_interval(days => %s)
          AND wall_clock_s IS NOT NULL
    """, [days])
    n, total_s, avg_s, merged_s, merged_n = (rows[0] if rows else (0,) * 5)
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
    with get_db() as db:
        fuel(db, args.days)
        yield_(db, args.days)
        holding(db, args.days)
        cost(db, args.days)
    print("\n" + "=" * 62)
    print("  No P&L verdict appears here on purpose: the desk's MDE is")
    print("  8.84-11.63pp on 46 lifetime fills, so patch-level trading")
    print("  impact is unmeasurable for ~a year. See power_report.py.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
