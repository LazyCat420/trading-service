"""Sweep the policy confidence floor against realized outcomes.

`_apply_policy_gates` blocks BUY/SELL below CONFIDENCE_FLOOR. That number was
raised 65 -> 70 on measured evidence (c949c57); this re-derives it from the
current outcome history and reports what each candidate floor would have done.
The gate is live: `app/v3/orchestrator.py` reads it as
`get_param("ANALYSIS_CONFIDENCE_THRESHOLD")`, whose ParamSpec is
`default=70, min_value=50, max_value=90` — which is exactly the 50..90 step-5
range swept below, so no candidate this prints is out of bounds.

READS MONGODB. Ported off Postgres 2026-08-30. It previously reached the
archive through `SIM_DSN`, and the port is not cosmetic:

  * `SIM_DSN` is not in `.env`, so the module died at IMPORT on
    `KeyError: 'SIM_DSN'` and this calibration has answered nothing since the
    2026-08-19 cutover. Nobody noticed, because a shelved instrument is
    indistinguishable from one nobody ran.
  * Had the variable been set, the failure would have been worse than a crash.
    The archive's newest `decision_outcomes` row is 2026-08-19 22:56:58 and
    every row in it is frozen there, so the sweep would have re-derived a LIVE
    POLICY GATE from a stale population, printing the same confident table.
  * The frozen half is not merely smaller, it is WRONG about rows it does hold.
    Three of the four clean BUY/SELL rows this port newly admits were created
    BEFORE the cutover and resolved AFTER it (do-2ee3f21b30ea +1.32 WIN,
    do-46ce583de666 -1.19 LOSS, do-bfe576b3e1fa -0.40 FLAT). Postgres still
    records all three as unresolved, with `pnl_pct` NULL — forever. A reader
    bound to it does not just miss new decisions, it misgrades old ones.

Parity, measured 2026-08-30 (`decision_outcomes`, clean BUY/SELL population):
Postgres 1775 rows, Mongo 1779; all 1775 archive rows present in Mongo, zero
field disagreements on the shared ids; the corrupt-population count is 371 in
both stores. SUPERSET, which is the pass.

The metric that matters is TOTAL P&L KEPT, not win rate. A floor that raises
win rate by discarding profitable-but-noisy trades is a worse floor -- the
system is not paid in win rate. Per-trade expectancy alone is also wrong: it
is trivially maximized by a floor of 99 that admits three trades a year.

CRITICAL -- the corrupt population. `decision_outcomes` contains rows whose
own lesson_stored reads "PIPELINE FAILURE (EMPTY_SIGNAL)" or "Failed to parse
thesis", plus rows with confidence=0: 351 and 363 respectively on 2026-08-30,
371 distinct resolved rows between them. These are pipeline failures scored as
trades, and they are NOT a random sample: among BUY/SELL they win 50.0% at
-5.63% avg P&L versus 61.1% / +1.88% for clean rows. Including them drags every
low band down and would flatter any floor. They are excluded here and the
exclusion is reported, because a calibration that silently drops a third of its
data is indistinguishable from one that cherry-picks.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_query  # noqa: E402

COLLECTION = "decision_outcomes"

#: The two lesson texts that mark a pipeline failure scored as a trade, as one
#: alternation. Case-SENSITIVE on purpose: SQL `LIKE` is case-sensitive and
#: `$regex` without `$options: "i"` is too. Adding the flag happens to change
#: nothing on 2026-08-30 (both spellings return 1779), which is not a reason to
#: add it -- it would widen the exclusion the first time a lesson is written in
#: another case, and that is a change to the calibration population.
_FAILED_LESSON = {"$regex": "PIPELINE FAILURE|Failed to parse"}

#: `WHERE resolved_at IS NOT NULL AND confidence > 0 AND pnl_pct IS NOT NULL
#:  AND COALESCE(lesson_stored,'') NOT LIKE '%PIPELINE FAILURE%'
#:  AND COALESCE(lesson_stored,'') NOT LIKE '%Failed to parse%'`
#:
#: The `lesson_stored` clause is the one that does not translate by eye. The
#: SQL COALESCEs a NULL lesson to '' and then asks NOT LIKE, so a decision that
#: stored no lesson at all is KEPT. In Mongo the field is null or absent on 185
#: of the clean rows, and the two obvious negations both drop every one of
#: them: `{"$ne": None}` and `{"$nin": [None, ""]}` are membership tests that a
#: missing field fails. `{"$not": {...}}` is the complement — it matches a
#: document the inner expression does not match, INCLUDING one that lacks the
#: field, which is exactly what COALESCE-to-'' buys. Measured 2026-08-30:
#: the `$not` form returns 1779 rows, either membership form 1594 -- the
#: same 185 lesson-less decisions missing, with no error either way.
CLEAN_QUERY = {
    "resolved_at": {"$ne": None},
    "confidence": {"$gt": 0},
    "pnl_pct": {"$ne": None},
    "lesson_stored": {"$not": _FAILED_LESSON},
}

#: The mirror image, reported as `excluded as corrupt`. `OR` -> `$or`, and no
#: `pnl_pct` clause: the original counted these regardless of whether they
#: resolved to a P&L.
CORRUPT_QUERY = {
    "resolved_at": {"$ne": None},
    "$or": [{"confidence": 0}, {"lesson_stored": _FAILED_LESSON}],
}

#: SELECT order. `summarize()` and the band loop index these positionally.
ROW_COLUMNS = ["action", "confidence", "pnl_pct", "outcome"]

TRADE_ACTIONS = ["BUY", "SELL"]


def load(collection: str = COLLECTION) -> tuple[list[tuple], int]:
    """The clean BUY/SELL population and the corrupt-row count it excludes."""
    rows = mongo_query.find_rows(
        collection,
        dict(CLEAN_QUERY, action={"$in": TRADE_ACTIONS}),
        ROW_COLUMNS,
    )
    excluded = mongo_query.count(collection, CORRUPT_QUERY)
    return rows, excluded


def summarize(rows: list, floor: int) -> dict:
    """What the desk would have kept at this floor."""
    admitted = [r for r in rows if r[1] >= floor]
    blocked = [r for r in rows if r[1] < floor]
    if not admitted:
        return {}
    pnls = [r[2] for r in admitted]
    wins = sum(1 for r in admitted if r[3] == "WIN")
    decided = sum(1 for r in admitted if r[3] in ("WIN", "LOSS"))
    return {
        "floor": floor,
        "admitted": len(admitted),
        "pct_admitted": 100.0 * len(admitted) / len(rows),
        "win_pct": (100.0 * wins / decided) if decided else float("nan"),
        "avg_pnl": sum(pnls) / len(pnls),
        "total_pnl": sum(pnls),
        # P&L left on the table by blocking. A floor is only justified if the
        # trades it blocks were collectively LOSING money.
        "blocked_pnl": sum(r[2] for r in blocked),
        "blocked_n": len(blocked),
    }


def main() -> None:
    action_filter = sys.argv[1].upper() if len(sys.argv) > 1 else "ALL"

    rows, excluded = load()

    if action_filter in ("BUY", "SELL"):
        rows = [r for r in rows if r[0] == action_filter]

    if not rows:
        raise SystemExit("no clean resolved decisions -- cannot calibrate")

    print(f"action={action_filter}  clean n={len(rows)}  "
          f"excluded as corrupt={excluded}\n")
    print(f"{'floor':>5} {'admitted':>9} {'%kept':>7} {'win%':>7} "
          f"{'avg P&L':>9} {'total P&L':>11} {'blocked P&L':>12} {'blkd n':>7}")
    print("-" * 76)

    best = None
    for floor in range(50, 91, 5):
        s = summarize(rows, floor)
        if not s:
            continue
        # Rank by total P&L kept: the quantity the desk is actually paid in.
        if best is None or s["total_pnl"] > best["total_pnl"]:
            best = s
        print(f"{s['floor']:>5} {s['admitted']:>9} {s['pct_admitted']:>6.1f}% "
              f"{s['win_pct']:>6.1f}% {s['avg_pnl']:>8.2f}% "
              f"{s['total_pnl']:>10.0f} {s['blocked_pnl']:>11.0f} "
              f"{s['blocked_n']:>7}")

    if best is None:
        # Every candidate floor admitted nothing -- possible only if the whole
        # population sits below 50. Say so instead of raising TypeError on
        # `best['floor']`, which is what the unported version did.
        raise SystemExit("no floor in 50..90 admits a single decision")

    print(f"\nbest total P&L at floor={best['floor']} "
          f"(keeps {best['pct_admitted']:.0f}% of trades, "
          f"blocks {best['blocked_n']} worth {best['blocked_pnl']:.0f})")

    # Per-band expectancy: where does the sign actually flip? A floor placed
    # anywhere inside a positive-expectancy band is discarding money.
    print(f"\n{'band':>8} {'n':>6} {'win%':>7} {'avg P&L':>9} {'total':>9}")
    print("-" * 44)
    bands = defaultdict(list)
    for action, conf, pnl, outcome in rows:
        lo = min(int(conf // 5 * 5), 90)
        bands[lo].append((pnl, outcome))
    for lo in sorted(bands):
        vals = bands[lo]
        decided = [o for _, o in vals if o in ("WIN", "LOSS")]
        wins = sum(1 for o in decided if o == "WIN")
        pnls = [p for p, _ in vals]
        wp = (100.0 * wins / len(decided)) if decided else float("nan")
        print(f"{lo:>4}-{lo+4:<3} {len(vals):>6} {wp:>6.1f}% "
              f"{sum(pnls)/len(pnls):>8.2f}% {sum(pnls):>8.0f}")


if __name__ == "__main__":
    main()
