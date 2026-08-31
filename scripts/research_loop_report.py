#!/usr/bin/env python3
"""Is the open-ended research loop doing anything?

Reports the two things that are answerable in days rather than against the
desk's 8.84pp minimum detectable effect:

  1. THE QUESTION LEDGER — how many questions the desk raises, how many it
     raises AGAIN (research did not answer them), how many it stops raising,
     and how many were actually answered with evidence.

  2. THE WORKLIST SHADOW — how far the research queue's proposed universe is
     from the one the cycle actually analysed, and from top_scorers[:N].

Both halves read MongoDB. Postgres froze at the 2026-08-19 cutover and its
`worklist_shadow_runs` stops at 109 rows / 2026-08-19 22:40 UTC; Mongo carries
those same 109 cycles plus everything since (243 documents on 2026-08-30).

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

A shadow read that FAILS now says so and exits 1. It used to print
`query failed: ...` immediately above "(worklist_shadow_runs is created on
first record() call)", which reads as "nothing has been recorded yet" — and
that is exactly what it printed for the eight days after the cutover, when the
real cause was an AttributeError for a settings field the cutover deleted, and
92 cycles had in fact been recorded. A dead instrument must not be mistakable
for an idle subsystem.

    python3 scripts/research_loop_report.py [--days 14]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import mongo_query, mongo_store  # noqa: E402

# A POSTGRES TABLE NAME, never a resolved collection name: every mongo_store /
# mongo_query helper calls collection_for() itself, exactly once.
SHADOW = "worklist_shadow_runs"


def _cutoff(days: int) -> datetime:
    """`CURRENT_TIMESTAMP - (%s || ' days')::interval`, as a tz-aware UTC stamp.

    tz-aware on purpose: `worklist_shadow.record()` stamps
    `datetime.now(timezone.utc)`, BSON stores it as UTC, and pymongo hands it
    back naive. Comparing against a naive `utcnow()` would happen to work here
    and would silently shift by the host's offset the first time this ran
    anywhere but UTC.
    """
    return datetime.now(timezone.utc) - timedelta(days=int(days))


def _per_row_ratio(numerator: str) -> dict:
    """`<numerator>::float / NULLIF(budget, 0)` for ONE document.

    Under `$avg` this is the mean of the per-cycle ratios, which is what
    `avg(a::float / NULLIF(b, 0))` computes — and NOT `sum(a) / sum(b)`. The
    two disagree whenever the budget varies between cycles, and it does vary
    (1..7 across the archive), so the convenient form would report a
    budget-weighted number under the label of an unweighted one.

    The `$cond` is the NULLIF. A zero — or missing — budget yields null, and
    `$avg` skips nulls exactly as SQL's `avg()` skips NULLs. Coalescing to 0
    instead would drag the mean down by one term per undivideable row, which
    is the shape of an answer that looks like a finding.
    """
    return {"$cond": [
        {"$and": [{"$ne": ["$budget", None]}, {"$ne": ["$budget", 0]}]},
        {"$divide": [f"${numerator}", "$budget"]},
        None,
    ]}


def shadow_pipeline(cutoff: datetime) -> list[dict]:
    """The five shadow aggregates, in the SELECT order the SQL used.

    `sql_to_mongo` refuses this statement — "AVG over an expression" — so it is
    written out here rather than approximated by a helper that cannot express
    it. `mongo_query.agg_row` cannot either: its ("avg", col) vocabulary takes
    a field, not a quotient.
    """
    return [
        {"$match": {"created_at": {"$gte": cutoff}}},
        {"$group": {
            "_id": None,
            "n": {"$sum": 1},
            "avg_budget": {"$avg": "$budget"},
            "ov_free": {"$avg": _per_row_ratio("overlap_live_free")},
            "ov_queue": {"$avg": _per_row_ratio("overlap_live_queue")},
            # sum(CASE WHEN queue_empty THEN 1 ELSE 0 END): a missing or null
            # flag takes the ELSE branch, as it does in SQL.
            "empty": {"$sum": {"$cond": [{"$eq": ["$queue_empty", True]}, 1, 0]}},
        }},
    ]


def shadow_row(days: int) -> tuple:
    """`cursor.fetchone()` for the shadow aggregate — a 5-tuple in SELECT order.

    An empty window returns SQL's answer for an empty group, `(0, None, None,
    None, None)`, and not an empty tuple: the caller unpacks five values.
    """
    rows = mongo_store.aggregate(SHADOW, shadow_pipeline(_cutoff(days)))
    if not rows:
        return (0, None, None, None, None)
    d = rows[0]
    return (d.get("n") or 0, d.get("avg_budget"), d.get("ov_free"),
            d.get("ov_queue"), d.get("empty"))


def undated_shadow_docs() -> int:
    """Documents with NO `created_at` — invisible to every window, by design.

    `{"$gte": cutoff}` does not match a missing field, and Postgres's
    `created_at >= ...` did not match a NULL either, so excluding them is the
    faithful translation. It is printed rather than assumed because the count
    is not zero: 39 of 243 documents on 2026-08-30 carry no stamp (all of them
    `cycle_id='test-cycle'`, written 2026-08-18, and none of them ever reached
    Postgres). Postgres defaulted this column to CURRENT_TIMESTAMP and Mongo
    does not, so the number can only grow — and a window that quietly drops
    rows is how a real gap gets read as a quiet week.
    """
    return mongo_store.count_docs(SHADOW, {"created_at": {"$exists": False}})


def question_section(days: int) -> dict:
    from app.services.question_ledger import stats

    s = stats(days=days)
    print(f"\n{'=' * 66}")
    print(f"QUESTION LEDGER — last {days} days")
    print("=" * 66)

    if not s["total"]:
        print("  no rows. Either the dossier sync has not shipped/run, or no")
        print("  agent emitted sub_analyses_requested in the window.")
        print("  Check: mongo_store.count_docs('dossier_question_log', {})")
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
        n, avg_budget, ov_free, ov_queue, empty = shadow_row(days)
        undated = undated_shadow_docs()
    except Exception as e:
        out["error"] = str(e)
        print(f"  READ FAILED: {e}")
        print("  This is an ERROR, not an empty result — the shadow may well")
        print("  have recorded cycles that this run could not see. Exit 1.")
        return out

    out["rows"] = int(n or 0)
    if not out["rows"]:
        print("  no rows — no cycle has run since the shadow shipped.")
        if undated:
            print(f"  ({undated} document(s) carry no created_at and are")
            print("   outside every window — see undated_shadow_docs().)")
        return out

    print(f"  cycles recorded ............. {out['rows']}")
    print(f"  mean universe size .......... {float(avg_budget or 0):.1f}")
    print(f"  live ∩ free  (top_scorers) .. {float(ov_free or 0):.1%}")
    print(f"  live ∩ queue (research) ..... {float(ov_queue or 0):.1%}")
    print(f"  cycles with an EMPTY queue .. {int(empty or 0)} of {out['rows']}")
    if undated:
        print(f"  undated documents ........... {undated} (no created_at; "
              "outside every window)")

    out["overlap_free"] = float(ov_free or 0)
    out["overlap_queue"] = float(ov_queue or 0)
    out["empty"] = int(empty or 0)
    out["undated"] = int(undated)

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
            payload["shadow_rows"] = mongo_query.count(
                SHADOW, {"created_at": {"$gte": _cutoff(args.days)}})
            payload["shadow_undated"] = undated_shadow_docs()
        except Exception as e:
            payload["shadow_rows"] = None
            payload["shadow_error"] = str(e)
        print(json.dumps(payload, indent=2))
        return 1 if payload.get("shadow_error") else 0

    question_section(args.days)
    shadow = shadow_section(args.days)
    print()
    return 1 if shadow.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
