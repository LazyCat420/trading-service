#!/usr/bin/env python3
"""Is the open-ended research loop doing anything?

Reports the two things that are answerable in days rather than against the
desk's 8.84pp minimum detectable effect:

  1. THE QUESTION LEDGER — how many questions the desk raises, how many it
     raises AGAIN (research did not answer them), how many it stops raising,
     and how many were actually answered with evidence.

  2. THE WORKLIST SHADOW — how far the research queue's proposed universe is
     from the one the cycle actually analysed, and from top_scorers[:N].

## Read this before quoting a number from it

`answered` is zero until the deep-dive queue is served (Track A2). That zero
means "research is queued but not running yet", NOT "research answered
nothing". The script prints the distinction rather than letting a reader infer
the wrong one.

`dropped` is NOT a resolution. A question can leave the artifact because it was
answered, because a different agent ran, or because the model did not repeat
itself. Summing `dropped` into a success rate makes the metric pass whether the
loop works or not.

`reask_rate` is the signal available today, and it is unambiguous: a re-asked
question is one research demonstrably did not close.

    python3 scripts/research_loop_report.py [--days 14]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.migration.pg_connection import get_db  # noqa: E402


def question_section(days: int) -> dict:
    from app.services.question_ledger import stats

    s = stats(days=days)
    print(f"\n{'=' * 66}")
    print(f"QUESTION LEDGER — last {days} days")
    print("=" * 66)

    if not s["total"]:
        print("  no rows. Either the dossier sync has not shipped/run, or no")
        print("  agent emitted sub_analyses_requested in the window.")
        print("  Check: SELECT count(*) FROM dossier_question_log;")
        return s

    print(f"  distinct questions .......... {s['total']}")
    for status in ("open", "reasked", "dropped", "answered", "aged_out"):
        count = s["by_status"].get(status, 0)
        pct = 100.0 * count / s["total"]
        print(f"    {status:<10} {count:>6}  ({pct:5.1f}%)")
    print(f"  deepest re-ask chain ........ {s['max_ask_count']}")

    if s["reask_rate"] is not None:
        print(f"\n  RE-ASK RATE ................. {s['reask_rate']:.1%}")
        print("    the share of questions the desk raised again. Unambiguous:")
        print("    research did not close these.")

    if not s["answered"]:
        print("\n  ANSWERED = 0.")
        print("    Expected while the deep-dive queue has no consumer (A2 not")
        print("    shipped). This is 'not running yet', not 'found nothing'.")
        print("    It becomes a finding only once a research worker is live.")
    return s


def shadow_section(days: int) -> dict:
    print(f"\n{'=' * 66}")
    print(f"WORKLIST SHADOW — last {days} days")
    print("=" * 66)
    out: dict = {"rows": 0}
    try:
        with get_db() as db:
            rows = db.execute(
                """
                SELECT count(*),
                       avg(budget),
                       avg(overlap_live_free::float / NULLIF(budget, 0)),
                       avg(overlap_live_queue::float / NULLIF(budget, 0)),
                       sum(CASE WHEN queue_empty THEN 1 ELSE 0 END)
                  FROM worklist_shadow_runs
                 WHERE created_at >= CURRENT_TIMESTAMP - (%s || ' days')::interval
                """,
                [int(days)],
            ).fetchone()
    except Exception as e:
        print(f"  query failed: {e}")
        print("  (worklist_shadow_runs is created on first record() call)")
        return out

    n, avg_budget, ov_free, ov_queue, empty = rows or (0, None, None, None, 0)
    out["rows"] = int(n or 0)
    if not out["rows"]:
        print("  no rows — no cycle has run since the shadow shipped.")
        return out

    print(f"  cycles recorded ............. {out['rows']}")
    print(f"  mean universe size .......... {float(avg_budget or 0):.1f}")
    print(f"  live ∩ free  (top_scorers) .. {float(ov_free or 0):.1%}")
    print(f"  live ∩ queue (research) ..... {float(ov_queue or 0):.1%}")
    print(f"  cycles with an EMPTY queue .. {int(empty or 0)} of {out['rows']}")

    out["overlap_free"] = float(ov_free or 0)
    out["overlap_queue"] = float(ov_queue or 0)
    out["empty"] = int(empty or 0)

    if out["empty"] == out["rows"]:
        print("\n  The queue was empty on every cycle. An empty queue overlaps")
        print("  nothing, so live∩queue = 0% here means 'no queue', not")
        print("  'total disagreement'. Nothing can be concluded until the")
        print("  dossier sync has enqueued across a few cycles.")
    elif out["overlap_free"] > 0.9:
        print("\n  live ≈ free: the LLM gatekeeper is reproducing top_scorers[:N].")
        print("  That is the finding open item 10 predicts — score it before")
        print("  spending anything else on its transport.")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.json:
        from app.services.question_ledger import stats
        payload = {"questions": stats(days=args.days)}
        try:
            with get_db() as db:
                r = db.execute(
                    "SELECT count(*) FROM worklist_shadow_runs "
                    "WHERE created_at >= CURRENT_TIMESTAMP - (%s || ' days')::interval",
                    [int(args.days)],
                ).fetchone()
            payload["shadow_rows"] = int(r[0]) if r else 0
        except Exception as e:
            payload["shadow_rows"] = None
            payload["shadow_error"] = str(e)
        print(json.dumps(payload, indent=2))
        return 0

    question_section(args.days)
    shadow_section(args.days)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
