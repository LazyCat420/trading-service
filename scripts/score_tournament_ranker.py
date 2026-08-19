#!/usr/bin/env python3
"""Score the tournament as a RANKER, not as a probability forecaster.

WHY THIS EXISTS
---------------
`scripts/score_panel.py` scored the tournament with Brier and got 0.3090 versus
0.2266 for the base rate — worse than a coin flip — and the standing conclusion
was "the tournament is noise, delete it and take back the ~203s/ticker".

That conclusion does not follow from that number. Brier is a *calibration*
score: it punishes a forecaster whose stated probabilities are wrong, even when
its *ordering* is right. A model that says 0.9 for everything it likes and 0.8
for everything it dislikes scores terribly on Brier while ranking perfectly.

Measured 2026-07-29, the tournament's ordering is not noise:

    bear-flagged tickers   -2.85% over ~5 sessions   up-rate 34%   n=102
    bull-flagged tickers   -0.49%                    up-rate 56%   n= 75
    Mann-Whitney p=0.0072      up-rate OR=2.44 p=0.0056

So it is BADLY CALIBRATED and DIRECTIONALLY DISCRIMINATING at the same time.
Those are different properties, and only one of them is what the board consumes
— the board reads `winning_side`, a label, not `confidence`.

Deleting it on the Brier number alone would have thrown away the strongest
single predictor of board action measured in this codebase (bear-win -> board
HOLD 74% vs bull-win 30%, Fisher OR=6.5 p=3.2e-09).

WHAT THIS REPORTS
-----------------
Rank-based statistics that are invariant to calibration:
  * separation      — mean/median forward return, bull vs bear
  * up-rate + Fisher exact odds ratio
  * Mann-Whitney U  — the nonparametric "do these come from the same
                      distribution" test; no normality assumption, and returns
                      are famously not normal
  * AUC             — P(a random bull outranks a random bear). 0.5 = no skill.
                      This IS the ranking quality, stated as one number.

Deliberately NOT reported: Brier. If you want calibration, use score_panel.py —
this script exists precisely because the two questions were being conflated.

Usage:
    python scripts/score_tournament_ranker.py --since 2026-06-18 --horizon 5
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Below this the separation is inside the noise band and no verdict is issued.
MIN_N_PER_SIDE = 30


def load(since: str, horizon: int) -> dict[str, list[float]]:
    """winning_side -> [forward return %] for every scorable desk."""
    from scripts.migration.pg_connection import get_db

    with get_db() as db:
        rows = db.execute(
            """
            SELECT s.ticker, s.created_at::date,
                   s.desk_data->'tournament_result'->>'winning_side'
            FROM shared_desk s
            WHERE s.created_at >= %s
              AND s.desk_data->'tournament_result' IS NOT NULL
            """,
            [since],
        ).fetchall()

        out: dict[str, list[float]] = defaultdict(list)
        for ticker, day, side in rows:
            if side not in ("bull", "bear"):
                continue  # skipped / veto / fallback are not directional calls
            r = db.execute(
                """
                WITH e AS (SELECT close FROM price_history
                            WHERE ticker = %s AND date <= %s ORDER BY date DESC LIMIT 1),
                     x AS (SELECT close FROM price_history
                            WHERE ticker = %s AND date >  %s ORDER BY date ASC
                            OFFSET %s LIMIT 1)
                SELECT (SELECT close FROM e), (SELECT close FROM x)
                """,
                [ticker, day, ticker, day, max(horizon - 1, 0)],
            ).fetchone()
            if r and r[0] and r[1] and float(r[0]) > 0:
                out[side].append(100.0 * (float(r[1]) - float(r[0])) / float(r[0]))
    return out


def auc(pos: list[float], neg: list[float]) -> float | None:
    """P(random pos > random neg) via the Mann-Whitney U identity, ties=0.5."""
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return wins / (len(pos) * len(neg))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-06-18")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    data = load(args.since, args.horizon)
    bull, bear = data.get("bull", []), data.get("bear", [])

    print(f"═══ TOURNAMENT AS A RANKER (since {args.since}, horizon {args.horizon}) ═══\n")
    if not bull or not bear:
        print("  no directional calls scorable — nothing to report")
        return 1

    for label, v in (("bull", bull), ("bear", bear)):
        up = sum(1 for x in v if x > 0)
        print(f"  {label:5} n={len(v):4d}  mean={statistics.mean(v):+6.2f}%  "
              f"median={statistics.median(v):+6.2f}%  up={100 * up / len(v):.0f}%")

    sep = statistics.mean(bull) - statistics.mean(bear)
    print(f"\n  separation (bull - bear): {sep:+.2f} percentage points")

    a = auc(bull, bear)
    print(f"  AUC: {a:.3f}   (0.500 = no ranking skill)")

    result = {"n_bull": len(bull), "n_bear": len(bear), "separation": round(sep, 3),
              "auc": round(a, 4) if a else None}

    try:
        from scipy.stats import fisher_exact, mannwhitneyu

        u, p = mannwhitneyu(bull, bear, alternative="greater")
        print(f"  Mann-Whitney U={u:.0f}  p={p:.4f}  (H1: bull ranks above bear)")
        tab = [[sum(1 for x in bull if x > 0), sum(1 for x in bull if x <= 0)],
               [sum(1 for x in bear if x > 0), sum(1 for x in bear if x <= 0)]]
        orr, pf = fisher_exact(tab)
        print(f"  up-rate Fisher OR={orr:.2f}  p={pf:.4f}")
        result |= {"mannwhitney_p": round(float(p), 5),
                   "fisher_or": round(float(orr), 3), "fisher_p": round(float(pf), 5)}
    except ImportError:
        print("  (scipy unavailable — significance tests skipped)")
        p = None

    print("\n═══ VERDICT ═══")
    if min(len(bull), len(bear)) < MIN_N_PER_SIDE:
        print(f"  needs-more-data: fewer than {MIN_N_PER_SIDE} on one side.")
    elif p is not None and p < 0.05 and a and a > 0.5:
        print("  The tournament RANKS. Its ordering separates winners from losers")
        print("  at p<0.05, regardless of how badly its stated confidence is")
        print("  calibrated. Do NOT delete it on the Brier score alone —")
        print("  the board consumes `winning_side`, a label, not `confidence`.")
        print("\n  Worth doing instead: recalibrate the CONFIDENCE (isotonic /")
        print("  Platt on this same data) and keep the ranking untouched.")
    else:
        print("  No ranking skill distinguishable from noise at this n.")
        print("  If this holds as n grows, the delete case is then real —")
        print("  but it must be made on THIS number, not on Brier.")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
