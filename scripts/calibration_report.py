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

READS MONGODB. Ported off Postgres 2026-08-30, and the port is not cosmetic:

  * The read reached the archive through the migration package's connection
    helper, whose pool asks the settings object for an archive DSN field that
    was deleted on 2026-08-28. Every run since has raised AttributeError
    before printing a single line, so this report has answered nothing for the
    whole of the post-cutover period.
  * Had the DSN survived, the failure would have been quieter and worse. The
    archive's newest `decision_outcomes` row is 2026-08-19 22:56:58 and is
    frozen there, so the sweep would have kept re-deriving a LIVE policy floor
    from a dead population while printing the same confident table.
  * The frozen copy is also WRONG about rows it does hold. Three pre-cutover
    BUYs were resolved after it (do-bfe576b3e1fa -0.40, do-46ce583de666 -1.19,
    do-2ee3f21b30ea +1.32); Postgres still records all three as unresolved
    with `pnl_pct` NULL, forever, so a reader bound to it does not merely miss
    new decisions — it misgrades old ones.

Parity, measured 2026-08-30 over this exact population (the archive SQL run
against Postgres vs. the query below run against Mongo):

    BUY   pg n=907 mean +2.7607   mongo n=911 mean +2.7443
          0 rows only in pg, 4 only in Mongo (3 resolved after the cutover,
          1 created after it) -> SUPERSET, which is the pass
    SELL  pg n=868 mean +0.9831   mongo n=868 mean +0.9831, identical row for
          row in created_at order -> MATCH

The excluded corrupt population is identical in both stores: confidence=0 363,
lesson_stored ~ 'PIPELINE FAILURE' 145, ~ 'Failed to parse' 206.

REPRODUCING THE ANCHOR. The collection is live and grows, so a bare run can
never return the 2026-07-26 numbers again. `--as-of DATE` restricts the
population to decisions RESOLVED on or before DATE — exactly what this script
could have seen on the day it was run. `--as-of 2026-07-26` returns, from
Mongo on 2026-08-30:

    n=829  null=+2.88   <72: n=136 mean -1.84 (-4.72 vs null)
                       >=72: n=693 mean +3.80 (+0.93 vs null)

One row wider in the low band than the recorded 135, and 0.03pp apart on the
high band. It does not reconstruct to the row, and cannot: outcomes are
re-resolved after the fact and `pnl_pct` is rewritten when they are, so the
population "as of 2026-07-26" is not a thing any later store can rebuild
exactly. Both bands, both signs and both gaps reproduce; that is the claim.

Usage:
    python scripts/calibration_report.py
    python scripts/calibration_report.py --action BUY --min-n 30
    python scripts/calibration_report.py --as-of 2026-07-26
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mongo_query  # noqa: E402
from app.quant.stat_gates import (  # noqa: E402
    is_oos_degradation,
    newey_west_tstat,
    stationary_bootstrap_ci,
)

BANDS = ((0, 60), (60, 66), (66, 72), (72, 78), (78, 85), (85, 101))
CANDIDATE_THRESHOLDS = (60, 65, 68, 70, 72, 75, 78)

COLLECTION = "decision_outcomes"

#: The two lesson texts that mark a pipeline failure scored as a trade, as one
#: alternation — `LIKE '%PIPELINE FAILURE%' OR LIKE '%Failed to parse%'`.
#: Neither string contains a regex metacharacter, so the substring semantics of
#: LIKE and of an unanchored `$regex` are the same here. Case-SENSITIVE on
#: purpose, because `LIKE` is: `$options: "i"` would widen the exclusion the
#: first time a lesson is written in another case, and that is a change to the
#: calibration population, not a tidy-up. Same constant as
#: `scripts/calibrate_confidence_floor.py`, deliberately — the two reports must
#: not disagree about which rows are real decisions.
_FAILED_LESSON = {"$regex": "PIPELINE FAILURE|Failed to parse"}


def clean_query(action: str, as_of: dt.datetime | None = None) -> dict:
    """The WHERE clause of the archive SELECT, as a Mongo filter.

    `WHERE resolved_at IS NOT NULL AND action = %s AND pnl_pct IS NOT NULL
     AND confidence IS NOT NULL AND confidence > 0
     AND COALESCE(lesson_stored,'') NOT LIKE '%PIPELINE FAILURE%'
     AND COALESCE(lesson_stored,'') NOT LIKE '%Failed to parse%'`

    Only the `lesson_stored` clause fails to translate by eye, and it is the
    one that decides the whole finding. The SQL COALESCEs a NULL lesson to ''
    and then asks NOT LIKE, so a decision that stored no lesson at all is
    KEPT. In Mongo that field is null on 679 documents and ABSENT on 56 more,
    and the two obvious negations drop every one of them: `{"$ne": None}` and
    `{"$nin": [None, ""]}` are membership tests that a missing field fails.
    `{"$not": {...}}` is the complement — it matches any document the inner
    expression does not match, including one that lacks the field — which is
    exactly what COALESCE-to-'' buys. Measured 2026-08-30 on BUY: the `$not`
    form returns 911 rows and either membership form 778 — the same 133
    lesson-less decisions missing (185 across BUY and SELL), with no error on
    either side and nothing in the output that looks wrong.

    `IS NOT NULL` -> `{"$ne": None}` is right for the rest: post-cutover
    documents can lack `pnl_pct` and `resolved_at` entirely (35 of each today,
    unresolved decisions), and `$ne: None` excludes a missing field just as
    `IS NOT NULL` excluded a NULL.
    """
    resolved: dict = {"$ne": None}
    if as_of is not None:
        resolved["$lt"] = as_of
    return {
        "resolved_at": resolved,
        "action": action,
        "pnl_pct": {"$ne": None},
        "confidence": {"$ne": None, "$gt": 0},
        "lesson_stored": {"$not": _FAILED_LESSON},
    }


def fetch(action: str, as_of: dt.datetime | None = None) -> list[tuple[int, float]]:
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

    WHICH CLAUSE DOES THE WORK, measured 2026-08-30. Almost all of it is
    `confidence > 0`: drop that alone and SELL 0-59 goes from n=37 mean -3.27%
    straight back to n=393 mean -5.53%, i.e. the recorded disaster band. The
    two `NOT LIKE` clauses add only 8 rows across both actions, because a row
    whose lesson names the failure is nearly always a confidence=0 row too.
    They are kept because "nearly always" is not always, and because the
    exclusion is quoted in this docstring as two rules; a reader who trusts
    the sentence should get the rows the sentence describes.

    Rows come back in `created_at` order because the chronological split below
    is the check the finding rests on. Sorting is only chronological while
    `created_at` is a BSON Date: a string timestamp sorts above every Date and
    would quietly deal the halves wrong rather than raise, so the type is
    counted and a violation is reported instead of averaged over.
    """
    strings = mongo_query.count(COLLECTION, {"created_at": {"$type": "string"}})
    if strings:
        print(f"⚠ {strings} {COLLECTION} documents store created_at as a STRING, "
              f"not a Date.\n  BSON sorts every string above every date, so the "
              f"chronological split below\n  is NOT chronological. Fix the "
              f"timestamps before reading the halves.", file=sys.stderr)

    rows = mongo_query.find_rows(
        COLLECTION,
        clean_query(action, as_of),
        ["confidence", "pnl_pct"],
        sort=[("created_at", 1)],
    )
    return [(int(c), float(p)) for c, p in rows]


def _as_of(value: str) -> dt.datetime:
    """`--as-of 2026-07-26` -> the exclusive upper bound 2026-07-27T00:00Z.

    Inclusive of the named day, because the population a run on that day saw
    is everything resolved BY the end of it.
    """
    day = dt.datetime.strptime(value, "%Y-%m-%d").date()
    return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(),
                               tzinfo=dt.timezone.utc)


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
    ap.add_argument("--as-of", type=_as_of, metavar="YYYY-MM-DD", default=None,
                    help="Only decisions RESOLVED on or before this date — what "
                         "a run on that day would have seen. Use it to "
                         "reproduce a recorded measurement against a "
                         "collection that has grown since.")
    args = ap.parse_args()

    rows = fetch(args.action, args.as_of)
    if len(rows) < args.min_n:
        print(f"Only {len(rows)} resolved {args.action} decisions — not enough to "
              f"calibrate anything.")
        return 0

    pnl = np.array([p for _, p in rows])
    null = float(pnl.mean())

    print("=" * 88)
    print(f"CONFIDENCE CALIBRATION — {len(rows)} resolved {args.action} decisions")
    print("=" * 88)
    if args.as_of is not None:
        cutoff = (args.as_of - dt.timedelta(days=1)).date()
        print(f"AS OF {cutoff} — only decisions resolved on or before that day.")
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
