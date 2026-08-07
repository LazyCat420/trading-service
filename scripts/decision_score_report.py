#!/usr/bin/env python3
"""Two reports for the deterministic baseline score.

    python -m scripts.decision_score_report distribution
    python -m scripts.decision_score_report shadow

DISTRIBUTION scores the whole universe from the rows on file and prints the
composite's spread and the band split. Run it after ANY change to the weights,
the anchors or the band cuts — the logic reads fine whichever way it is
calibrated, and only the distribution tells you whether the thing filters or
just labels. The first cut of the band thresholds put 77% of 881 tickers into
one band, which is the exact failure the module was written to fix, and
nothing but this report would have shown it.

SHADOW joins `decision_scores` against `decision_outcomes` and asks the
question the shadow period exists to answer: does the computed baseline rank
realised P&L better than the board's own confidence? Until that has an answer
the score stays advisory — it is injected into the prompt and stored, and no
consumer reads the band back into an action.

Note what this CANNOT do: it cannot backfill. `fundamentals` is not a
point-in-time panel — rows are overwritten as vendors refresh — so a score
recomputed for a past decision is not the score that desk was shown. The
shadow report only has data from the moment recording started, and a
`decision_scores` row with a NULL `board_action` is a desk that never reached
a verdict, which is a different fact from a HOLD.
"""

import statistics
import sys
from collections import Counter


def _universe_scores():
    from app.quant.decision_score import compute_decision_score, rank_scores
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            "SELECT DISTINCT ticker FROM fundamentals ORDER BY ticker"
        ).fetchall()
    tickers = [r[0] for r in rows]
    print(f"scoring {len(tickers)} tickers...", file=sys.stderr)
    scores = [compute_decision_score(t) for t in tickers]
    return rank_scores(scores)


def distribution() -> int:
    scores = _universe_scores()
    scoreable = [s for s in scores if s.get("score") is not None]
    if not scoreable:
        print("nothing scoreable — check that fundamentals/technicals have rows")
        return 1

    vals = sorted(s["score"] for s in scoreable)

    def pct(q):
        return round(vals[min(len(vals) - 1, int(q * len(vals)))], 1)

    print(f"\nscored {len(scoreable)} of {len(scores)} "
          f"({len(scores) - len(scoreable)} NOT_SCOREABLE)")
    print(f"composite  min={vals[0]}  p5={pct(.05)}  p25={pct(.25)}  "
          f"p50={pct(.50)}  p75={pct(.75)}  p95={pct(.95)}  max={vals[-1]}")
    print(f"           mean={statistics.mean(vals):.1f}  "
          f"stdev={statistics.stdev(vals):.1f}  distinct={len(set(vals))}")

    print("\nbands")
    total = len(scores)
    for band, n in Counter(s["band"] for s in scores).most_common():
        print(f"  {band:18} {n:5}  {100.0 * n / total:5.1f}%")
    # The check that matters. One band holding most of the universe means the
    # cuts label rather than filter, whatever the code says.
    top = Counter(s["band"] for s in scores).most_common(1)[0]
    if top[1] / total > 0.70:
        print(f"\n  WARNING: {top[0]} holds {100.0 * top[1] / total:.0f}% of "
              f"the universe — these cuts are not filtering. Re-cut them on "
              f"the percentiles above.")

    confs = sorted(s["confidence"] for s in scoreable)
    print(f"\nbaseline confidence  min={confs[0]}  p50={confs[len(confs) // 2]}"
          f"  max={confs[-1]}  distinct={len(set(confs))}")
    if len(set(confs)) < 10:
        print("  WARNING: fewer than 10 distinct values — a scorer that emits "
              "a handful of values ranks nothing.")

    rrs = sorted(s["risk_reward"]["ratio"] for s in scoreable
                 if s["risk_reward"].get("ratio") is not None)
    if rrs:
        print(f"\nrisk/reward  n={len(rrs)}  p10={rrs[len(rrs) // 10]:.2f}  "
              f"p50={rrs[len(rrs) // 2]:.2f}  "
              f"p90={rrs[9 * len(rrs) // 10]:.2f}  max={rrs[-1]:.2f}")

    print("\ngate firings (a gate that fires on almost nothing or almost "
          "everything is not a gate)")
    for name, n in Counter(g for s in scores
                           for g in (s.get("gates_failed") or [])).most_common():
        print(f"  FAIL    {name:16} {n:5}  {100.0 * n / total:5.1f}%")
    for name, n in Counter(g for s in scores
                           for g in (s.get("gates_unknown") or [])).most_common():
        print(f"  UNKNOWN {name:16} {n:5}  {100.0 * n / total:5.1f}%")

    print("\ntop 15 by composite")
    for s in sorted(scoreable, key=lambda x: -x["score"])[:15]:
        rr = s["risk_reward"].get("ratio")
        print(f"  {s['ticker']:8} {s['score']:5g}  {s['band']:18} "
              f"conf={s['confidence']:<3} R:R={rr if rr is not None else '-'}")
    return 0


def shadow() -> int:
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            """
            -- `do` is a RESERVED WORD in Postgres (the DO statement), so the
            -- obvious alias for decision_outcomes is a syntax error and this
            -- whole subcommand raised before it read a row. It shipped that
            -- way in aac14ec and stayed dead until 2026-08-07, because the
            -- only test on this file covers compute_decision_score, never the
            -- reporter. `outcome` is not reserved.
            SELECT ds.band, ds.score, ds.baseline_confidence, ds.risk_reward,
                   ds.board_action, ds.board_confidence, outcome.pnl_pct
              FROM decision_scores ds
              LEFT JOIN decision_outcomes outcome
                     ON outcome.cycle_id = ds.cycle_id
                    AND outcome.ticker = ds.ticker
             WHERE ds.score IS NOT NULL
            """
        ).fetchall()

    if not rows:
        print("no decision_scores rows yet — the shadow period has not started "
              "(or the service has not run a cycle since deploy)")
        return 0

    print(f"\n{len(rows)} baseline rows recorded")
    undecided = sum(1 for r in rows if r[4] is None)
    print(f"  {undecided} have NO board action — desks that never reached a "
          f"verdict. NOT the same as a HOLD.")

    print("\nbaseline band vs the action the desk took")
    pairs = Counter((r[0], r[4] or "NO_DECISION") for r in rows)
    for (band, action), n in pairs.most_common():
        print(f"  {band:18} -> {action:12} {n:5}")

    # ── Dispersion, paired ──────────────────────────────────────────────────
    # This needs NO resolved outcomes, so it runs before the early return
    # below — and it is the comparison that diagnosed the all-HOLD desk on
    # 2026-08-07. Both numbers describe the SAME ticker in the SAME cycle off
    # the SAME stored rows, so a difference in spread cannot be the market,
    # the universe, or the data: only the thing that produced the number.
    # Measured that day: baseline sd 13.76 over 28-84 with 10/74 at >=80,
    # against board sd 4.73 over 55-74 with 0/74 at >=80 — and means that
    # matched to within 0.2. The desk had not become uncertain, it had become
    # unable to be certain.
    paired = [(float(r[2]), float(r[5])) for r in rows
              if r[2] is not None and r[5] is not None]
    if len(paired) >= 10:
        base = [p[0] for p in paired]
        board = [p[1] for p in paired]
        print(f"\nconfidence dispersion, paired on (cycle, ticker), n={len(paired)}")
        for label, vals in (("baseline (free, deterministic)", base),
                            ("board    (LLM)", board)):
            print(f"  {label:32} mean={statistics.mean(vals):5.1f} "
                  f"sd={statistics.pstdev(vals):5.2f} "
                  f"range={min(vals):.0f}-{max(vals):.0f} "
                  f">=80: {sum(1 for v in vals if v >= 80)}/{len(vals)}")
        print(f"  mean gap (baseline - board): "
              f"{statistics.mean(b - d for b, d in paired):+.1f} — a gap near "
              f"zero with very different sd is a SCALE problem, not a "
              f"disagreement about the names.")
    else:
        print(f"\nconfidence dispersion: only {len(paired)} paired rows — "
              f"need 10 to say anything.")

    resolved = [r for r in rows if r[6] is not None]
    if len(resolved) < 30:
        print(f"\n{len(resolved)} resolved outcomes — too few to rank on. "
              f"The shadow period needs more data before the comparison below "
              f"means anything; do not promote the score on this.")
        return 0

    print(f"\nmean P&L by baseline band (n={len(resolved)} resolved)")
    by_band: dict[str, list[float]] = {}
    for r in resolved:
        by_band.setdefault(r[0], []).append(float(r[6]))
    for band, vals in sorted(by_band.items(),
                             key=lambda kv: -statistics.mean(kv[1])):
        print(f"  {band:18} n={len(vals):4}  mean={statistics.mean(vals):+.2f}%")

    # The comparison the shadow period exists for. Both are reported as a
    # simple split at their own median so the two are on equal footing — a
    # correlation would be dominated by the P&L tail.
    for label, idx in (("baseline composite", 1),
                       ("baseline confidence", 2),
                       ("board confidence", 5)):
        vals = [(float(r[idx]), float(r[6])) for r in resolved
                if r[idx] is not None]
        if len(vals) < 30:
            print(f"\n{label}: only {len(vals)} rows — skipped")
            continue
        vals.sort()
        mid = len(vals) // 2
        lo = statistics.mean(v[1] for v in vals[:mid])
        hi = statistics.mean(v[1] for v in vals[mid:])
        print(f"\n{label}: bottom half {lo:+.2f}%  top half {hi:+.2f}%  "
              f"spread {hi - lo:+.2f}pp  (n={len(vals)})")
    print("\nA spread is not significance. Before acting on any of this, run "
          "it through a permutation test and re-check on executable decisions "
          "only — a blocked SELL on an unheld ticker is not a trade, and "
          "scoring those as losses has produced a retracted headline here "
          "before.")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "distribution"
    if mode == "distribution":
        return distribution()
    if mode == "shadow":
        return shadow()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
