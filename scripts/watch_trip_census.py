#!/usr/bin/env python3
"""Export and label the recent Watch Desk trips. Read-only.

Checklist item 2 of the research-allocator plan: take the last N trips, join
each to the cycle it started and the decision that cycle produced, and label it.

WHY THE LABELS ARE COMPUTED, NOT TYPED
--------------------------------------
A hand-typed label is a transcription, and a transcription drifts from the rows
it describes with nothing able to catch it. Every label below is derived from
the row it labels, so re-running this on a later window relabels correctly
rather than repeating a judgement made about different data.

The one judgement that CANNOT be computed is "is this headline about the open
question" — the watches predate the resolution-condition contract, so there is
nothing on the record to compare against. That is reported as
`off_thesis: unknown` rather than guessed. A census that guessed there would be
inventing the very measurement the contract exists to make possible.

Usage:
    python scripts/watch_trip_census.py [--limit 30] [--out docs/watch_desk/trips_labelled.json]
    python scripts/watch_trip_census.py --fixtures tests/fixtures/watch_desk/
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REAL_ACTIONS = {"BUY", "SELL", "HOLD"}


def _db():
    from app.db.mongo import get_mongo_client
    from app.db.mongo_store import TRADING_MONGO_DB

    return get_mongo_client()[TRADING_MONGO_DB]


def _action(row) -> str | None:
    if not row:
        return None
    rj = row.get("result_json")
    try:
        rj = json.loads(rj) if isinstance(rj, str) else (rj or {})
    except (ValueError, TypeError):
        return None
    return ((rj.get("action") or "").upper()) or None


def _cycle_of(cmd) -> str | None:
    """The pipeline cycle a wd- command actually started.

    `watch_events.cycle_id` is the COMMAND id, not the cycle id. Without this
    hop every join to a decision comes back empty and the census would report
    that no wake ever produced anything.
    """
    if not cmd or not cmd.get("result"):
        return None
    try:
        parsed = ast.literal_eval(cmd["result"]) if isinstance(cmd["result"], str) else cmd["result"]
        return (parsed or {}).get("cycle_id")
    except (ValueError, SyntaxError, TypeError, AttributeError):
        return None


def census(limit: int = 30) -> list[dict]:
    db = _db()
    out = []
    for e in db.watch_events.find({}, sort=[("fired_at", -1)], limit=limit):
        cmd = db.v3_system_commands.find_one({"id": e.get("cycle_id")}) if e.get("cycle_id") else None
        cycle = _cycle_of(cmd)
        woke = db.analysis_results.find_one({"cycle_id": cycle, "ticker": e["ticker"]}) if cycle else None
        prior = db.analysis_results.find_one(
            {"ticker": e["ticker"], "created_at": {"$lt": e["fired_at"]}},
            sort=[("created_at", -1)])
        summary = db.cycle_run_summaries.find_one({"cycle_id": cycle}) if cycle else None
        watch = db.ticker_watches.find_one({"id": e.get("watch_id")})

        prior_action, woke_action = _action(prior), _action(woke)
        row = {
            "event_id": e.get("id"), "watch_id": e.get("watch_id"),
            "ticker": e.get("ticker"), "trigger_type": e.get("trigger_type"),
            "detail": e.get("detail"), "fired_at": e.get("fired_at"),
            "command_id": e.get("cycle_id"), "command_status": (cmd or {}).get("status"),
            "cycle_id": cycle, "cycle_status": (summary or {}).get("status"),
            "prior_action": prior_action, "woke_action": woke_action,
            "prior_at": (prior or {}).get("created_at"),
            "hours_since_prior": (
                round((e["fired_at"] - prior["created_at"]).total_seconds() / 3600, 1)
                if prior and prior.get("created_at") else None),
            "watch_has_resolution_condition": bool(
                (watch or {}).get("decision_context", {}).get("resolution_condition")
                or (watch or {}).get("resolution_condition")),
            "watch_schema_version": int((watch or {}).get("schema_version") or 0),
        }

        # ── Labels, all derived ──
        labels = []
        if row["trigger_type"] == "staleness":
            labels.append("scheduled")          # a clock, not an event
        else:
            labels.append("evidence_backed")
        if woke_action is None:
            labels.append("no_decision_produced")
        if prior_action not in REAL_ACTIONS:
            # A DEGRADED/absent prior is an error state, not a decision. A
            # "changed" verdict against one compares a decision to a failure.
            labels.append("prior_not_a_decision")
        elif woke_action is not None:
            labels.append("changed_decision" if woke_action != prior_action
                          else "no_decision_change")
        if row["cycle_status"] in ("error", "stopped"):
            labels.append("cycle_failed")
        if row["hours_since_prior"] is not None and row["hours_since_prior"] < 24:
            labels.append("reanalysed_within_24h")
        if not row["watch_has_resolution_condition"]:
            labels.append("no_resolution_condition")
        row["labels"] = labels
        # Not computable for legacy watches — see the module docstring.
        row["off_thesis"] = "unknown" if not row["watch_has_resolution_condition"] else None
        out.append(row)
    return out


def summarise(rows: list[dict]) -> dict:
    lab = collections.Counter(l for r in rows for l in r["labels"])
    return {
        "n": len(rows),
        "trigger_types": dict(collections.Counter(r["trigger_type"] for r in rows)),
        "labels": dict(lab),
        "no_decision_rate": round(lab["no_decision_produced"] / max(1, len(rows)), 3),
        "prior_not_a_decision_rate": round(lab["prior_not_a_decision"] / max(1, len(rows)), 3),
        "resolution_condition_coverage": round(
            sum(1 for r in rows if r["watch_has_resolution_condition"]) / max(1, len(rows)), 3),
    }


def daily_wake_counts(days: int = 21) -> dict:
    """Wakes per trading day — the measurement that showed the saturation."""
    db = _db()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    c = collections.Counter(
        e["fired_at"].date().isoformat()
        for e in db.watch_events.find({"fired_at": {"$gte": since}}, {"fired_at": 1})
        if e.get("fired_at"))
    return dict(sorted(c.items()))


def write_fixtures(rows: list[dict], out_dir: str) -> str:
    """Freeze the census as replay fixtures.

    CAPTURED ONCE, THEN FROZEN. A fixture regenerated from the code under test
    turns the gate into a tautology forever: the replay would assert that today's
    allocator agrees with today's allocator. These rows come from `watch_events`,
    which no part of the allocator writes, so they stay an independent oracle.
    Re-run with --fixtures ONLY to extend the window, never to "fix" a red.
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "live_trips.json")
    if os.path.exists(path):
        print(f"REFUSING to overwrite existing fixture {path}.\n"
              f"  A frozen oracle that the code under test can regenerate is not an oracle.\n"
              f"  Delete it deliberately if you truly mean to re-capture.", file=sys.stderr)
        return path
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "note": "Frozen from live watch_events. Do not regenerate to fix a test.",
        "summary": summarise(rows),
        "daily_wake_counts": daily_wake_counts(),
        "trips": rows,
    }
    with open(path, "w") as f:
        json.dump(payload, f, default=str, indent=1)
    print(f"wrote {path} ({len(rows)} trips)")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--out", default="docs/watch_desk/trips_labelled.json")
    ap.add_argument("--fixtures", default=None,
                    help="also freeze the census as replay fixtures in this dir")
    args = ap.parse_args()

    rows = census(args.limit)
    summary = summarise(rows)
    daily = daily_wake_counts()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"summary": summary, "daily_wake_counts": daily, "trips": rows},
                  f, default=str, indent=1)

    print(json.dumps(summary, indent=1))
    print("\nwakes/day:")
    for k, v in daily.items():
        print(f"  {k}  {v}")
    print(f"\nwrote {args.out}")

    if args.fixtures:
        write_fixtures(rows, args.fixtures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
