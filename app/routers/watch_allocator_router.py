"""Watch-allocator dashboard API — why it woke, why it ran, what changed, cost.

The five columns are the deliverable, and each maps to something the desk could
not answer before:

  why it woke        — the evidence and its source timestamp, not just a
                       trigger type. "news" was the whole answer previously.
  why it ran/deferred— the score, the floor it was measured against, and every
                       component separately. An unexplainable ranking is how a
                       saturated budget went unnoticed for 21 days.
  what changed       — the prior decision vs the woken one, with a prior of
                       DEGRADED reported as NOT A DECISION rather than compared.
  cost               — wakes spent, and whether the wake bought anything.
  decision changed   — filled in by scripts/watch_allocator_outcomes.py after
                       the cycle finishes; ABSENT until then, never 0.

`outcome: null` means "not yet scored", and the payload says so in its own
field. A dashboard that rendered null as "no change" would report a confident
zero for every cycle still running.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query

from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watch-desk", tags=["watch-allocator"])

TRIAGE_LOG = "watch_triage_log"

_COLUMNS = [
    "id", "created_at", "mode", "ticker", "watch_id", "schema_version",
    "has_resolution_condition", "open_question", "trigger_type", "event_key",
    "evidence", "catalyst", "eligible", "reject_reason", "score", "score_floor",
    "components", "would_action", "would_reason", "clamps", "fired", "cycle_id",
    "outcome", "budget_left", "analyses_this_week",
]


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


@router.get("/allocations")
def allocations(
    hours: int = Query(72, ge=1, le=24 * 30),
    ticker: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    """Recent allocator decisions, newest first."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    q: dict = {"created_at": {"$gte": since}}
    if ticker:
        q["ticker"] = ticker.upper().strip()

    try:
        rows = mongo_query.find_rows(TRIAGE_LOG, q, _COLUMNS,
                                     sort=[("created_at", -1)], limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.error("[WatchAllocatorRouter] read failed: %s", e)
        return {"status": "error", "message": str(e), "allocations": []}

    out = []
    for r in rows:
        d = dict(zip(_COLUMNS, r))
        d["created_at"] = _iso(d.get("created_at"))
        ev = d.get("evidence") or {}
        if isinstance(ev, dict):
            ev = {**ev, "observed_at": _iso(ev.get("observed_at"))}
        d["evidence"] = ev
        # Say WHY the outcome is missing rather than letting a null be read as
        # a measured "nothing changed".
        d["outcome_state"] = "scored" if d.get("outcome") else (
            "pending" if d.get("fired") else "not_applicable")
        out.append(d)

    return {"status": "ok", "window_hours": hours, "n": len(out),
            "summary": _summarise(out), "allocations": out}


def _summarise(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    fired = [r for r in rows if r.get("fired")]
    scored = [r for r in fired if r.get("outcome")]
    changed = [r for r in scored if (r.get("outcome") or {}).get("decision_changed")]
    no_dec = [r for r in scored if not (r.get("outcome") or {}).get("woke_action")]
    reasons: dict = {}
    for r in rows:
        if r.get("fired"):
            continue
        k = r.get("reject_reason") or r.get("would_reason") or "unknown"
        reasons[k] = reasons.get(k, 0) + 1

    def _rate(num, den):
        # A rate over an empty denominator is not 0 — it is unmeasured.
        return round(num / den, 3) if den else None

    return {
        "n": len(rows),
        "fired": len(fired),
        "deferred": len(rows) - len(fired),
        "deferral_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "outcomes_scored": len(scored),
        "outcomes_pending": len(fired) - len(scored),
        "decision_changed": len(changed),
        "decision_change_rate": _rate(len(changed), len(scored)),
        "no_decision_produced": len(no_dec),
        "no_decision_rate": _rate(len(no_dec), len(scored)),
        "resolution_condition_coverage": _rate(
            sum(1 for r in rows if r.get("has_resolution_condition")), len(rows)),
        "mean_score_fired": (
            round(sum(r.get("score") or 0 for r in fired) / len(fired), 3)
            if fired else None),
        "mean_score_deferred": (
            round(sum(r.get("score") or 0 for r in rows if not r.get("fired"))
                  / max(1, len(rows) - len(fired)), 3)
            if len(rows) > len(fired) else None),
    }


@router.get("/saturation")
def saturation(days: int = Query(21, ge=1, le=90)):
    """Wakes per day — the measurement that showed the budget was always full.

    Served alongside the allocator log so "did the allocator change how the
    budget is spent" is answerable from one place. A flat line at the budget
    ceiling is the pre-allocator signature.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        rows = mongo_query.find_rows(
            "watch_events", {"fired_at": {"$gte": since}}, ["fired_at", "ticker"],
            sort=[("fired_at", -1)], limit=2000)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": str(e), "per_day": {}}

    per_day: dict = {}
    for when, tick in rows:
        if not isinstance(when, datetime):
            continue
        per_day.setdefault(when.date().isoformat(), []).append(tick)

    counts = {k: len(v) for k, v in sorted(per_day.items())}
    values = list(counts.values())
    return {
        "status": "ok",
        "per_day": counts,
        # A single distinct value across many days IS the saturation signature.
        "saturated": len(set(values)) == 1 and len(values) >= 7,
        "distinct_daily_counts": sorted(set(values)),
    }
