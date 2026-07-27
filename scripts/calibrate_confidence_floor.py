"""Sweep the policy confidence floor against realized outcomes.

`_apply_policy_gates` blocks BUY/SELL below CONFIDENCE_FLOOR. That number was
raised 65 -> 70 on measured evidence (c949c57); this re-derives it from the
current outcome history and reports what each candidate floor would have done.

The metric that matters is TOTAL P&L KEPT, not win rate. A floor that raises
win rate by discarding profitable-but-noisy trades is a worse floor -- the
system is not paid in win rate. Per-trade expectancy alone is also wrong: it
is trivially maximized by a floor of 99 that admits three trades a year.

CRITICAL -- the corrupt population. `decision_outcomes` contains 366 rows whose
own lesson_stored reads "PIPELINE FAILURE (EMPTY_SIGNAL)" or "Failed to parse
thesis", plus 363 with confidence=0. These are pipeline failures scored as
trades, and they are NOT a random sample: they win 55.1% at -5.61% avg P&L
versus 61.1% / +1.94% for clean rows. Including them drags every low band
down and would flatter any floor. They are excluded here and the exclusion is
reported, because a calibration that silently drops a third of its data is
indistinguishable from one that cherry-picks.
"""

import os
import sys
from collections import defaultdict

import psycopg

DSN = os.environ["SIM_DSN"]

CLEAN_FILTER = """
    resolved_at IS NOT NULL
    AND confidence > 0
    AND COALESCE(lesson_stored, '') NOT LIKE '%%PIPELINE FAILURE%%'
    AND COALESCE(lesson_stored, '') NOT LIKE '%%Failed to parse%%'
    AND pnl_pct IS NOT NULL
"""

ROWS_SQL = f"""
    SELECT action, confidence, pnl_pct, outcome
    FROM decision_outcomes
    WHERE {CLEAN_FILTER} AND action IN ('BUY', 'SELL')
"""

EXCLUDED_SQL = """
    SELECT COUNT(*) FROM decision_outcomes
    WHERE resolved_at IS NOT NULL
      AND (confidence = 0
           OR COALESCE(lesson_stored, '') LIKE '%PIPELINE FAILURE%'
           OR COALESCE(lesson_stored, '') LIKE '%Failed to parse%')
"""


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

    with psycopg.connect(DSN) as conn:
        rows = conn.execute(ROWS_SQL).fetchall()
        excluded = conn.execute(EXCLUDED_SQL).fetchone()[0]

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
