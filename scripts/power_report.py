#!/usr/bin/env python3
"""What effect size this desk can actually detect — the measurement ceiling.

    python scripts/power_report.py
    python scripts/power_report.py --horizon 10 --executable-only

WHY THIS EXISTS
---------------
Every capability proposal on this repo is argued against a number like "the
minimum detectable effect is 2.24pp". That number was computed by hand on
2026-07-30 and could not be reproduced, re-run, or re-checked as the sample
grew — so the single constraint governing what is worth building was itself
unfalsifiable. This makes it a command.

WHAT IT REPORTS, AND WHY THE SECOND NUMBER IS THE REAL ONE
----------------------------------------------------------
The naive MDE treats every scored decision as an independent sample. It is not:
forward windows inside a short decision span overlap almost completely, and the
tickers are cross-correlated on top of that (PC1 ~47% of variance). The repo has
already been burned by this exact error — a HOLD-vs-BUY result of "-2.26pp,
p=0.0032" turned out to rest on roughly TWO independent windows.

So this prints both:

  MDE (naive)      — using n decisions. Optimistic. What a careless read gives.
  MDE (independent)— using non-overlapping windows. This is the honest ceiling.

A proposed signal must beat the INDEPENDENT figure to be detectable at all.

The formula is the standard two-sample comparison at alpha=0.05 (two-sided) and
power=0.80, assuming an equal split between the two arms:

    MDE = (z(1-a/2) + z(power)) * sd * sqrt(2 / (n/2))

which reproduces the 2.24pp figure from sd=5.0pp, n=157.
"""

from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.connection import get_db  # noqa: E402

Z_ALPHA_2 = 1.959964  # two-sided 0.05
Z_POWER = 0.8416212  # 80%


def mde(sd: float, n: int, z_power: float = Z_POWER) -> float:
    """Smallest difference in means detectable at alpha=.05, given power.

    Two-sample, equal allocation: each arm holds n/2. Returns NaN below n=2,
    where the question is not defined rather than merely underpowered.
    """
    if n < 2 or sd <= 0 or not math.isfinite(sd):
        return float("nan")
    per_arm = n / 2.0
    return (Z_ALPHA_2 + z_power) * sd * math.sqrt(2.0 / per_arm)


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def independent_windows(dates: list, horizon: int) -> int:
    """Non-overlapping forward windows the decision span can hold.

    The same counting `agent_scorecard.py` prints as INDEPENDENCE. A 5-week
    span at a 10-session horizon holds ~4 of them, no matter how many desks ran.
    """
    if not dates:
        return 0
    span = (max(dates) - min(dates)).days
    return max(1, int(span / max(horizon, 1)) + 1)


def intraclass_correlation(clusters: list[list[float]]) -> float:
    """ICC across time blocks, by one-way ANOVA.

    Decisions taken in the same window share the market move, so they are not
    independent draws. rho measures how much of the variance is BETWEEN blocks
    rather than within them. rho=0 means the desks are independent given the
    block; rho=1 means a block contributes exactly one distinct observation.
    """
    groups = [g for g in clusters if len(g) >= 1]
    n_total = sum(len(g) for g in groups)
    k = len(groups)
    if k < 2 or n_total <= k:
        return float("nan")

    grand = sum(sum(g) for g in groups) / n_total
    ss_between = sum(len(g) * (sum(g) / len(g) - grand) ** 2 for g in groups)
    ss_within = sum((x - sum(g) / len(g)) ** 2 for g in groups for x in g)
    ms_between = ss_between / (k - 1)
    ms_within = ss_within / (n_total - k)

    # Zero within-cluster variance is not an undefined ICC — it is the extreme
    # case of one. Every desk in a window shared an outcome exactly, so the
    # window carries a single distinct observation no matter how many ran.
    if ms_within <= 0:
        return 1.0 if ms_between > 0 else float("nan")

    # Average cluster size, Kish-corrected for unequal sizes.
    m0 = (n_total - sum(len(g) ** 2 for g in groups) / n_total) / (k - 1)
    if m0 <= 1:
        return float("nan")
    icc = (ms_between - ms_within) / (ms_between + (m0 - 1) * ms_within)
    return max(0.0, min(1.0, icc))


def effective_n(clusters: list[list[float]], rho: float) -> float:
    """Sample size after the design effect, n / (1 + (m-1)*rho).

    This is the number that belongs in a power calculation. The naive n treats
    1,785 clustered decisions as 1,785 draws; counting blocks alone throws away
    the cross-sectional breadth entirely. Neither is right — the design effect
    is the standard correction between them.
    """
    n_total = sum(len(g) for g in clusters)
    k = len([g for g in clusters if g])
    if not n_total or not k or not math.isfinite(rho):
        return float("nan")
    m = n_total / k
    deff = 1.0 + (m - 1.0) * rho
    return n_total / deff if deff > 0 else float("nan")


def fetch(include_degraded: bool) -> tuple[list[dict], int, int]:
    """Resolved outcomes, plus (degraded_count, fills_count)."""
    where = ["resolved_at IS NOT NULL", "pnl_pct IS NOT NULL"]
    if not include_degraded:
        # DEGRADED_ARTIFACT rows are pipeline failures scored as trades. They
        # are hypothetical — measured 2026-07-30, they carry ZERO fills — so
        # including them measures the pipeline's crash rate, not its judgement.
        #
        # Excluded BY DEFAULT since 2026-07-31. It was opt-in behind
        # --executable-only, which meant the default invocation quietly
        # reported the contaminated number and every other consumer that
        # copied this file's logic inherited the wrong default.
        where.append("outcome <> 'DEGRADED_ARTIFACT'")

    with get_db() as db:
        rows = [
            {"pnl_pct": float(r[0]), "created_at": r[1], "action": r[2], "outcome": r[3]}
            for r in db.execute(
                f"SELECT pnl_pct, created_at, action, outcome FROM decision_outcomes "
                f"WHERE {' AND '.join(where)}"
            ).fetchall()
            if r[0] is not None
        ]
        degraded = db.execute(
            "SELECT count(*) FROM decision_outcomes WHERE outcome = 'DEGRADED_ARTIFACT'"
        ).fetchone()[0]
        try:
            fills = db.execute("SELECT count(*) FROM trade_fills").fetchone()[0]
        except Exception:  # noqa: BLE001 — table may not exist in a bare DB
            fills = -1
    return rows, degraded, fills


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=int, default=10,
                    help="forward-window length in sessions (default 10)")
    ap.add_argument("--executable-only", action="store_true",
                    help="(default; kept for compatibility) exclude DEGRADED_ARTIFACT rows")
    ap.add_argument("--include-degraded", action="store_true",
                    help="include DEGRADED_ARTIFACT pipeline failures — measures "
                         "the crash rate alongside judgement, rarely what you want")
    args = ap.parse_args()

    rows, degraded, fills = fetch(args.include_degraded)
    if len(rows) < 2:
        print(f"Only {len(rows)} resolved outcome(s) — nothing to compute.")
        return 1

    pnl = [r["pnl_pct"] for r in rows]
    dates = sorted(
        (r["created_at"].date() if hasattr(r["created_at"], "date") else r["created_at"])
        for r in rows
    )
    sd = _stdev(pnl)
    n = len(pnl)
    blocks = independent_windows(dates, args.horizon)

    # Cluster the decisions into non-overlapping horizon-length blocks so the
    # correlation between same-window desks can be measured rather than assumed.
    origin = dates[0]
    buckets: dict[int, list[float]] = {}
    for r in rows:
        d = r["created_at"].date() if hasattr(r["created_at"], "date") else r["created_at"]
        buckets.setdefault((d - origin).days // max(args.horizon, 1), []).append(r["pnl_pct"])
    clusters = list(buckets.values())

    rho = intraclass_correlation(clusters)
    n_eff = effective_n(clusters, rho)

    naive = mde(sd, n)
    honest = mde(sd, n_eff) if math.isfinite(n_eff) else float("nan")
    blocks_only = mde(sd, blocks)

    print("=" * 72)
    print("MEASUREMENT CEILING" + ("  [INCLUDES DEGRADED]" if args.include_degraded else "  [EXECUTABLE ONLY]"))
    print("=" * 72)
    print(f"resolved outcomes ............... {n}")
    print(f"trade fills (entire history) .... {fills if fills >= 0 else 'n/a'}")
    print(f"DEGRADED_ARTIFACT rows .......... {degraded}"
          + ("  (INCLUDED — crash rate is in these numbers)" if args.include_degraded else "  (excluded)"))
    print(f"decision span ................... {dates[0]} .. {dates[-1]} "
          f"({(dates[-1] - dates[0]).days}d)")
    print(f"pnl_pct sd ...................... {sd:.2f}pp")
    print(f"time blocks @ h={args.horizon:<3} ............. {blocks}")
    print(f"intra-block correlation (rho) ... {rho:.3f}"
          if math.isfinite(rho) else "intra-block correlation (rho) ... n/a")
    print(f"effective n (design effect) ..... {n_eff:.1f}"
          if math.isfinite(n_eff) else "effective n (design effect) ..... n/a")
    print()
    print(f"MDE (naive, n={n}) {'.' * max(1, 18 - len(str(n)))} {naive:.2f}pp"
          "   <- optimistic: treats clustered desks as independent")
    if math.isfinite(honest):
        print(f"MDE (design-effect, n={n_eff:.0f}) ....... {honest:.2f}pp"
              "   <-- USE THIS")
    print(f"MDE (blocks only, n={blocks}) .......... {blocks_only:.2f}pp"
          "   <- pessimistic: discards cross-sectional breadth")
    print()

    ceiling = honest if math.isfinite(honest) else blocks_only
    if blocks < 10:
        print(f"⚠ Only {blocks} non-overlapping windows. Any p-value on this sample is")
        print("  weakly identified in TIME regardless of how many desks ran.")
    print(f"A new selection signal must move P&L by more than ~{ceiling:.2f}pp")
    print("  to be detectable at all. Below that it cannot be validated, only")
    print("  asserted — which is the argument for building risk/sizing controls")
    print("  (self-validating, e.g. Kupiec) rather than alpha claims.")

    # What it would take. Solving the MDE equation for n at a target effect.
    print()
    print("To detect a given effect, effective observations required:")
    for target in (1.0, 2.0, 3.0, 5.0):
        need = 2.0 * ((Z_ALPHA_2 + Z_POWER) * sd / target) ** 2
        print(f"  {target:>4.1f}pp -> {math.ceil(need):>7} effective obs")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
