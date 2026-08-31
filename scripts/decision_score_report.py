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

READS MONGODB (2026-08-30)
--------------------------
Both halves used to open the frozen archive through the migration pool, which
since the 2026-08-19 cutover raises AttributeError on a settings attribute
that no longer exists — so this file has not produced a number since the
cutover, loudly. (docs/PREFLIGHT_FAILED_OPEN_2026-08-30.md has the full list;
this script is one of its named three.) That is why `decision_scores` reads as
write-only: the collection has a writer (`app.quant.decision_score_store`) and
its ONLY reader is this script. The finding is closed from this end, by giving
the reader a live store; the collection is not the thing to delete.

Every read below names a POSTGRES TABLE and lets `mongo_store` resolve the
collection once, so a future rename cannot split the read from the write.

The `decision_scores`/`decision_outcomes` join is a COMPOSITE key
(cycle_id, ticker), which `mongo_query.left_join_rows` does not take — it
joins on one equality — so the stitch is done here, with the same semantics:
a NULL key matches nothing, an unmatched left row survives with the right
side NULL, and a left row matching N right rows emits N rows. Measured on the
archive: `decision_outcomes` has 239 (cycle_id, ticker) groups with more than
one row, so the fan-out is not hypothetical, even though none of those groups
happens to meet a scored desk today (237 matched rows from 237 distinct
`decision_scores` rows).

PARITY WITH THE ARCHIVE, AND WHY A NAIVE ROW DIFF CALLS IT WRONG
----------------------------------------------------------------
Measured 2026-08-30. `fundamentals` distinct tickers: pg 1,192, Mongo 1,212,
0 only-in-pg — a clean superset. `decision_scores WHERE score IS NOT NULL`:
pg 304, Mongo 490, 0 only-in-pg — likewise.

The joined result is NOT a clean superset, and the reason is worth knowing
before someone reports it as a port bug. Over the whole set, 51 rows are
"only in pg" — but every one of them is a row whose `pnl_pct` was NULL at the
cutover and has since RESOLVED: 53 of 53 pg-only `decision_outcomes` rows are
null-pnl in the archive and carry a number in Mongo for the SAME
(cycle_id, ticker), and 0 keys are only-in-pg. 111 outcomes resolved after
2026-08-19. So the archive holds an EARLIER STATE of rows Mongo still has,
which a row-identity diff can only express as "missing" — a value updated in
place is invisible to a comparison that only knows row-add.

Close the window on the field that actually moves and the two stores agree
exactly: scores created before the cutover joined to outcomes RESOLVED before
it gives 297 rows on each side, 0 only-in-pg, 0 only-in-mongo.

Historical note, because the test that guards it is still live: the SQL this
replaced aliased `decision_outcomes` as `do`, a Postgres reserved word, so
`shadow` raised a SyntaxError before reading a row from the day it shipped
(aac14ec) until 2026-08-07. `tests/unit/test_sql_reserved_aliases.py` keeps
that scan running over the SQL that is left elsewhere.
"""

import os
import statistics
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# The SELECT list the shadow report reads POSITIONALLY: r[0] band, r[1] score,
# r[2] baseline_confidence, r[3] risk_reward, r[4] board_action,
# r[5] board_confidence — and r[6] pnl_pct is stitched on from the right side.
# `risk_reward` is selected and never read; it is kept so the indices below
# are the SQL's indices, and dropping it would silently shift r[4] and r[5].
_SCORE_COLUMNS = ("band", "score", "baseline_confidence", "risk_reward",
                  "board_action", "board_confidence")


def _universe_tickers() -> list[str]:
    """`SELECT DISTINCT ticker FROM fundamentals ORDER BY ticker`.

    `sql_to_mongo` refuses SELECT DISTINCT ("use distinct_values() by hand"),
    so it is done by hand. Blank/None tickers are dropped rather than sorted
    against strings: `compute_decision_score("")` returns NOT_SCOREABLE, so
    keeping them would only pad the denominator every band percentage below is
    computed over. Measured 2026-08-30: 0 such documents in `fundamentals`,
    so today the two behaviours are identical.
    """
    from app.db import mongo_store

    return sorted({t for t in mongo_store.distinct_values("fundamentals", "ticker") if t})


def _universe_scores():
    from app.quant.decision_score import compute_decision_score, rank_scores

    tickers = _universe_tickers()
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


def _shadow_rows() -> list[tuple]:
    """The archive's

        SELECT ds.band, ds.score, ds.baseline_confidence, ds.risk_reward,
               ds.board_action, ds.board_confidence, outcome.pnl_pct
          FROM decision_scores ds
          LEFT JOIN decision_outcomes outcome
                 ON outcome.cycle_id = ds.cycle_id
                AND outcome.ticker  = ds.ticker
         WHERE ds.score IS NOT NULL

    as two collection reads and a Python stitch, returning tuples in the same
    SELECT order so every positional read below is unchanged.

    `{"score": {"$ne": None}}` is the faithful `IS NOT NULL`: in Mongo a query
    for `null` also matches a document that LACKS the field, so `$ne: None`
    excludes both — which is what a NULL column was. The mirror of that matters
    on the way out and is why the columns are read through `find_rows()` rather
    than filtered on: `board_action` and `board_confidence` are `$set` onto the
    row afterwards by `attach_board_decision`, so on a desk that never reached a
    verdict the fields are ABSENT, not null — 129 of 508 rows today, against 44
    explicit nulls inherited from the archive. `find_rows()` returns None for a
    missing field exactly as Postgres returned NULL for the column, so the
    "no board action" count below sees all 173 and not just the archive's 44.

    NOT `$lookup`: its left-outer array semantics differ from a LEFT JOIN, and
    `from:` would need a resolved collection name — a second `collection_for()`
    on a table `mongo_store` already resolves, which is the defect
    `tests/unit/test_no_double_collection_resolution.py` fails the build on.
    """
    from app.db import mongo_query

    left = mongo_query.find_rows(
        "decision_scores", {"score": {"$ne": None}},
        list(_SCORE_COLUMNS) + ["cycle_id", "ticker"])
    # The whole right side, as `left_join_rows` also reads it: 2,693 documents
    # of three fields. Pushing the left side's cycle_ids down as an `$in` would
    # be equivalent, and is not worth a second thing that can be wrong.
    right = mongo_query.find_rows(
        "decision_outcomes", {}, ["cycle_id", "ticker", "pnl_pct"])

    # `NULL = NULL` is not true, so a row without a COMPLETE key joins nothing,
    # on either side. That is enforced in ONE place — the index simply never
    # holds an incomplete key — and one place is the point: an incomplete left
    # key can only ever match an incomplete right key, so guarding the index
    # covers both sides, whereas guarding BOTH sides makes each guard redundant
    # and so removable with no test going red. Drop this condition and a
    # `decision_scores` row with no cycle_id joins every `decision_outcomes`
    # row with no cycle_id — a cross product of exactly the rows that should
    # not have matched at all, and one no row count would look wrong for.
    index: dict[tuple, list] = {}
    for cycle_id, ticker, pnl_pct in right:
        if cycle_id is not None and ticker is not None:
            index.setdefault((cycle_id, ticker), []).append(pnl_pct)

    rows: list[tuple] = []
    for row in left:
        head, key = row[:len(_SCORE_COLUMNS)], (row[-2], row[-1])
        # No match -> ONE row with the right side NULL (LEFT JOIN, not INNER);
        # N matches -> N rows, which is what the SQL did when the right side
        # was not unique on the key.
        for pnl_pct in (index.get(key) or [None]):
            rows.append(head + (pnl_pct,))
    return rows


def shadow() -> int:
    rows = _shadow_rows()

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
