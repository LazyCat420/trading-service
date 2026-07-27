#!/usr/bin/env python3
"""Is confidence calibrated, and where is the threshold that should bind?

The 2026-07-26 measurement this exists to keep alive: **the system cannot
reliably pick winners, but it CAN reliably identify its own bad decisions.**

    BUY confidence < 72 : n=135  mean -1.77%   -4.64% vs the always-long null
    BUY confidence >= 72: n=693  mean +3.77%   +0.90% vs the null

The low-confidence gap clears every gate in this repo — NW t=-6.31, bootstrap
p=0.0, and it holds in BOTH chronological halves independently (t=-4.34, -3.53).
The *positive* side does not: "high confidence beats the null" is t=1.21,
p=0.215, not significant. The gain comes from dropping losers, not from picking
winners, and this report is written to keep that distinction visible.

⚠ **The Deflated Sharpe is the WRONG tool for this finding and reports FAIL.**
DSR tests for a POSITIVE edge inflated by trial selection; this is a strongly
negative effect (Sharpe -0.38). The applicable checks are the chronological
split and IS/OOS, both printed below. Recorded so nobody later finds the FAIL
and reverses the threshold without reading why.

Every band is scored against the **always-long null over the same rows**, never
against zero. In a rising tape any long-biased strategy beats zero.

Usage:
    python scripts/calibration_report.py
    python scripts/calibration_report.py --action BUY --min-n 30
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.connection import get_db  # noqa: E402
from app.quant.stat_gates import (  # noqa: E402
    is_oos_degradation,
    newey_west_tstat,
    stationary_bootstrap_ci,
)

BANDS = ((0, 60), (60, 66), (66, 72), (72, 78), (78, 85), (85, 101))
CANDIDATE_THRESHOLDS = (60, 65, 68, 70, 72, 75, 78)


def fetch(action: str) -> list[tuple[int, float]]:
    """Resolved outcomes for `action`, EXCLUDING pipeline failures.

    `decision_outcomes` carries 366 rows that are not decisions at all: 363
    with confidence=0, and rows whose own lesson_stored reads "PIPELINE
    FAILURE (EMPTY_SIGNAL): Thesis returned confidence=0 with 0 claims" or
    "Failed to parse thesis. Invalid JSON format". The outcome tracker scored
    them as trades anyway.

    They are NOT a random sample — measured 2026-07-27, they win 55.1% at
    -5.61% mean versus 61.1% / +1.94% for real decisions — and they all land
    at confidence 0, i.e. inside the lowest band. Including them manufactures
    a huge fake "low confidence loses money" effect that has nothing to do
    with calibration: it is the pipeline's crash rate, mislabelled.

    Concretely, before this filter the SELL 0-59 band read n=392 mean -5.55%
    and the whole SELL sweep was dominated by it. A floor "justified" by that
    band would be gating on parse failures, not on confidence.
    """
    with get_db() as db:
        rows = db.execute(
            """
            SELECT confidence, pnl_pct
            FROM decision_outcomes
            WHERE resolved_at IS NOT NULL
              AND action = %s
              AND pnl_pct IS NOT NULL
              AND confidence IS NOT NULL
              AND confidence > 0
              AND COALESCE(lesson_stored, '') NOT LIKE '%%PIPELINE FAILURE%%'
              AND COALESCE(lesson_stored, '') NOT LIKE '%%Failed to parse%%'
            ORDER BY created_at
            """,
            [action],
        ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def _wilson(hits: int, n: int) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    z, p = 1.96, hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--action", default="BUY", choices=("BUY", "SELL"))
    ap.add_argument("--min-n", type=int, default=20,
                    help="Hide bands/thresholds thinner than this (default 20)")
    args = ap.parse_args()

    rows = fetch(args.action)
    if len(rows) < args.min_n:
        print(f"Only {len(rows)} resolved {args.action} decisions — not enough to "
              f"calibrate anything.")
        return 0

    pnl = np.array([p for _, p in rows])
    null = float(pnl.mean())

    print("=" * 88)
    print(f"CONFIDENCE CALIBRATION — {len(rows)} resolved {args.action} decisions")
    print("=" * 88)
    print(f"\nTHE NULL: always-long over these same rows = {null:+.2f}%")
    print("Every band below is scored against THAT, not against zero.\n")

    print(f"{'band':>10}{'n':>6}{'mean':>9}{'vs null':>9}{'win%':>7}{'95% CI':>14}")
    print("-" * 88)
    for lo, hi in BANDS:
        seg = [(c, p) for c, p in rows if lo <= c < hi]
        if len(seg) < args.min_n:
            if seg:
                print(f"{f'{lo}-{hi - 1}':>10}{len(seg):>6}   (thin — hidden)")
            continue
        vals = np.array([p for _, p in seg])
        wins = sum(1 for _, p in seg if p >= 1.0)
        losses = sum(1 for _, p in seg if p <= -1.0)
        directional = wins + losses
        wl, wh = _wilson(wins, directional)
        win_pct = 100.0 * wins / directional if directional else 0.0
        print(f"{f'{lo}-{hi - 1}':>10}{len(seg):>6}{vals.mean():>+9.2f}"
              f"{vals.mean() - null:>+9.2f}{win_pct:>6.0f}%"
              f"{100 * wl:>7.0f}-{100 * wh:<6.0f}%")

    # ── Where should the floor sit? ──
    print("\n" + "=" * 88)
    print("THRESHOLD SWEEP — how bad are the decisions BELOW each candidate floor?")
    print("=" * 88)
    print("A large negative gap with a significant t-stat means that floor is "
          "removing\nreal losses. Multiple thresholds are tested here, so treat "
          "the best one as\nfitted, not discovered — the chronological split "
          "below is the real check.\n")
    print(f"{'floor':>7}{'n below':>9}{'mean':>9}{'vs null':>9}{'NW t':>8}"
          f"{'boot p':>9}{'both':>7}")
    print("-" * 88)
    best = None
    for t in CANDIDATE_THRESHOLDS:
        below = np.array([p for c, p in rows if c < t])
        if below.size < args.min_n:
            continue
        gap = below - null
        nw = newey_west_tstat(gap, horizon=7)
        bs = stationary_bootstrap_ci(gap)
        both = bool(nw.get("passes") and bs.get("passes"))
        print(f"{t:>7}{below.size:>9}{below.mean():>+9.2f}{gap.mean():>+9.2f}"
              f"{nw.get('t_stat', 0):>8.2f}{bs.get('p_value', 1):>9.3f}"
              f"{('YES' if both else 'no'):>7}")
        # "Best" = most negative gap that clears both gates, i.e. the floor that
        # removes the most damage per decision while staying significant.
        if both and (best is None or gap.mean() < best[1]):
            best = (t, float(gap.mean()), below.size)

    live = None
    try:
        from app.services.parameter_store import get_param
        live = get_param("ANALYSIS_CONFIDENCE_THRESHOLD")
    except Exception as e:  # noqa: BLE001
        print(f"\n(could not read the live threshold: {e})")

    print()
    if best:
        t, gap, n = best
        print(f"FITTED FLOOR: {t} — decisions below it lose {gap:+.2f}% vs the "
              f"null (n={n})")
        if live is not None:
            print(f"LIVE THRESHOLD: {live}")
            if live < t:
                blocked = sum(1 for c, _ in rows if live <= c < t)
                print(f"  ⚠ {blocked} decisions sit between the live floor and the "
                      f"fitted one —\n    the gate currently lets them through.")
            elif live > t:
                print("  Live floor is ABOVE the fitted one — stricter than the "
                      "evidence requires.")
            else:
                print("  Live floor matches the fitted floor.")

        # The check that made this trustworthy: does it hold in both halves?
        below = np.array([p for c, p in rows if c < t]) - null
        mid = below.size // 2
        print("\nCHRONOLOGICAL SPLIT (the check that matters most — a threshold "
              "fitted to\none period and absent in the other is curve-fitting):")
        for label, seg in (("first half ", below[:mid]), ("second half", below[mid:])):
            if seg.size < 10:
                print(f"  {label}: too thin ({seg.size})")
                continue
            nw = newey_west_tstat(seg, horizon=7)
            print(f"  {label}: n={seg.size:3d} gap={seg.mean():+.2f}% "
                  f"t={nw.get('t_stat', 0):+.2f} "
                  f"{'PASS' if nw.get('passes') else 'FAIL'}")
        oos = is_oos_degradation(below)
        oos_note = oos.get("note") or "a negative Sharpe persisting OOS is the point here"
        print(f"  IS/OOS: {oos.get('is_sharpe')} -> {oos.get('oos_sharpe')}  ({oos_note})")

        kept = np.array([p for c, p in rows if c >= t])
        if kept.size:
            print(f"\nIF THAT FLOOR HAD BOUND: {kept.mean():+.2f}% per decision "
                  f"vs {null:+.2f}% actual  ({kept.mean() - null:+.2f}%)")
            print("  Achieved by REMOVING trades, not by finding better ones. The "
                  "ceiling of\n  this effect is the null itself.")
    else:
        print("No threshold clears both gates. Do not change the floor on this "
              "evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
