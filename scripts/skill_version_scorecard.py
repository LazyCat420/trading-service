#!/usr/bin/env python3
"""Did a skill version trade better than the one before it?

The question SkillOpt's own score gate cannot answer. That gate measures whether
a doc is better *written* — specific, actionable, not bloated. This measures
whether decisions made under it were better, which is the only justification for
the loop's cost.

**Read this before quoting any number below.**

1. **Against the always-long baseline, never zero.** An agent long in a rising
   tape looks brilliant against zero. The baseline is printed on every row.
2. **n will be small for a long time.** At ~7 decisions/cycle and a 25-decision
   maturity threshold, a version governs ~25-60 decisions. Detecting a ~1%
   per-decision edge needs hundreds. Expect "not distinguishable from the prior
   version" to be the honest answer for months — and note that the repo's own
   residual-alpha work already found no detectable alpha in the pipeline at
   n=106 (t=-0.904). A confident-looking difference at n=30 is noise.
3. **Sequential comparison is confounded by regime.** Version 20 in a rising
   week beats version 19 in a falling one regardless of quality. The
   baseline-relative column is the only one worth reading, and even it does not
   fully control for this. A true answer needs an A/B: two bots, different
   versions, same tickers, same cycles.

Usage:
    python scripts/skill_version_scorecard.py
    python scripts/skill_version_scorecard.py --agent v3_board_of_directors
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.migration.pg_connection import get_db  # noqa: E402


def _wilson(hits: int, n: int) -> tuple[float, float]:
    """95% Wilson interval — honest about small n, unlike hits/n alone."""
    if n <= 0:
        return (0.0, 0.0)
    z = 1.96
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", default=None, help="restrict to one agent")
    ap.add_argument("--min-n", type=int, default=5,
                    help="hide versions with fewer resolved decisions (default 5)")
    args = ap.parse_args()

    with get_db() as db:
        try:
            rows = db.execute(
                """
                SELECT key AS agent_name,
                       (value #>> '{}')::int AS version,
                       count(*)                                        AS n,
                       avg(pnl_pct)                                    AS avg_pnl,
                       sum(CASE WHEN outcome = 'WIN'  THEN 1 ELSE 0 END) AS wins,
                       sum(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                       min(created_at)::date                           AS first_seen,
                       max(created_at)::date                           AS last_seen
                FROM decision_outcomes d,
                     LATERAL jsonb_each(d.skill_versions)
                WHERE d.resolved_at IS NOT NULL
                  AND d.skill_versions IS NOT NULL
                  AND d.action IN ('BUY', 'SELL')
                GROUP BY 1, 2
                ORDER BY 1, 2
                """
            ).fetchall()
        except Exception as e:
            print(f"query failed: {e}")
            print("\nIf this says `skill_versions` does not exist, the migration "
                  "has not run on this database yet.")
            return 1

        # The null hypothesis: what staying long would have earned over the same
        # window. Without it a rising tape reads as skill.
        base_row = db.execute(
            "SELECT avg(pnl_pct) FROM decision_outcomes "
            "WHERE resolved_at IS NOT NULL AND action = 'BUY'"
        ).fetchone()
        baseline = float(base_row[0]) if base_row and base_row[0] is not None else None

    if not rows:
        print("No resolved decisions carry a skill version yet.\n")
        print("Expected until the stamp accrues: rows written before the "
              "2026-07-25 migration carry NULL, deliberately not backfilled.")
        print("A decision needs ~7 days to resolve, so the first usable rows "
              "land about a week after deploy.")
        return 0

    print(f"{'agent':26} {'ver':>4} {'n':>5} {'avg%':>7} {'vs base':>8} "
          f"{'win%':>6} {'95% CI':>14}  window")
    print("-" * 100)
    prev_agent = None
    for agent, version, n, avg_pnl, wins, losses, first_seen, last_seen in rows:
        if args.agent and agent != args.agent:
            continue
        if n < args.min_n:
            continue
        if prev_agent and prev_agent != agent:
            print()
        prev_agent = agent
        avg = float(avg_pnl or 0.0)
        directional = int(wins) + int(losses)
        lo, hi = _wilson(int(wins), directional)
        vs = f"{avg - baseline:+.2f}" if baseline is not None else "n/a"
        print(f"{agent:26} {version:>4} {n:>5} {avg:>+7.2f} {vs:>8} "
              f"{(100.0 * wins / directional if directional else 0):>5.0f}% "
              f"{100 * lo:>5.0f}-{100 * hi:<5.0f}%  {first_seen}..{last_seen}")

    print()
    if baseline is not None:
        print(f"BASELINE — always-long over all resolved BUYs: {baseline:+.2f}%")
    print("A version is only interesting if its interval clears the baseline AND "
          "does not overlap the prior version's. At the n values above, expect "
          "neither. Read the module docstring before drawing a conclusion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
