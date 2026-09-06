import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db import mongo_query
from app.db import mongo_store

logger = logging.getLogger(__name__)


def _audit_performance(cycle_id: str, cycle_summary: dict) -> dict:
    return {
        "total_ms": cycle_summary.get("elapsed_ms", 0),
        "tickers_analyzed": cycle_summary.get("analysis_results_count", 0),
        "collector_ok": cycle_summary.get("collector_ok", 0),
        "collector_skipped": cycle_summary.get("collector_skipped", 0),
        "collector_error": cycle_summary.get("collector_error", 0),
        "collector_late": cycle_summary.get("collector_late", 0),
        "collector_late_names": cycle_summary.get("collector_late_names", []),
        "collector_failures": cycle_summary.get("collector_failures", []),
        "trade_executed": cycle_summary.get("trade_executed", 0),
        "status": cycle_summary.get("status", "unknown"),
    }

#: How a stored line is named in `recovery_stats.by_type`. Ordered: the first
#: matching substring wins, so the more specific classes come first.
#:
#: Deliberately EXCLUDES "[ManagerAgent] ... took too much time". That timer
#: fires while an agent is working normally (Appendix I: 88% of the ERROR
#: stream is a deliberate non-abort, 23 of them on the acceptance cycle alone),
#: and counting it would make every healthy cycle read as a disaster — the
#: mirror of the defect this function exists to fix.
_RECOVERY_CLASSES: tuple[tuple[str, str], ...] = (
    ("all 5 attempts failed", "resilience_exhausted"),
    ("attempts failed", "resilience_exhausted"),
    ("CRASHED", "agent_crashed"),
    ("CANCELLED", "agent_cancelled"),
    ("TIMED OUT", "phase_timed_out"),
    ("stream stalled", "stream_stalled"),
    ("EMPTY RESPONSE", "empty_response"),
    ("Circuit breaker tripped", "circuit_breaker_tripped"),
    ("produced no parseable", "unparseable_artifact"),
    ("artifact repair", "artifact_repair"),
)

#: `pipeline_events.step` fragments that mark a recovery, mapped the same way.
_RECOVERY_STEPS: tuple[tuple[str, str], ...] = (
    ("retry_", "retry_attempt"),
    ("_crash_", "agent_crashed"),
    ("_cancelled_", "agent_cancelled"),
    ("board_degraded", "board_degraded"),
    ("debate_skipped", "debate_skipped"),
)


def _classify_recovery(text: str, table) -> str | None:
    for needle, name in table:
        if needle in text:
            return name
    return None


def _agent_in(text: str) -> str | None:
    """The agent a recovery line is about, if it names one."""
    import re

    m = re.search(r"\b(v3_[a-z0-9_]+|contradiction_shadow)\b", text)
    return m.group(1) if m else None


def _audit_recovery(cycle_id: str) -> dict:
    """What this cycle recovered from, rebuilt from what it actually logged.

    WHY IT IS NOT THE RECOVERY ENGINE ANY MORE. Measured 2026-09-06 across 329
    autoresearch reports in 40 days: `total_failures` was `0` in 247 and absent
    in 82 — never once non-zero — and `cycle_id` was `""` in 247 and missing in
    82 — never once set. It reported a clean recovery on a cycle with a crashed
    agent, an exhausted 5-attempt resilience budget, six 300 s stream stalls,
    two empty responses and 22 failed tool calls.

    `app/recovery/engine.py` advertises a `handle(FailureEvent)` entry point,
    but grepping the tree for `recovery_engine.` outside its own package
    returned exactly two lines — both here, both READS. One consumer, zero
    producers. Nothing called `reset_cycle` (the empty cycle_id) and nothing
    ever recorded a failure (the permanent zero).

    A blank field invites a question; a confident `0` closes it. This block is
    fed to the reflection prompt as `audit_bundle["recovery"]`, so the model
    writing the cycle's self-assessment was being told it recovered from
    nothing. Every one of those failures was already a row in
    `cycle_audit_log` or `pipeline_events`, both keyed by cycle_id.

    On an unreadable store this returns `total_failures: None` with an `error`,
    NOT zero — reporting a clean cycle because the database was down is the
    exact failure being removed.
    """
    from collections import Counter

    stats: dict = {
        "cycle_id": cycle_id or "",
        "total_failures": 0,
        "by_type": {},
        "by_agent": {},
        "resilience_exhausted": 0,
        "circuit_breakers_tripped": 0,
        "recent_events": [],
    }
    if cycle_id is None:
        return stats

    by_type: Counter = Counter()
    by_agent: Counter = Counter()
    events: list[dict] = []

    try:
        logs = mongo_store.find_docs(
            "cycle_audit_log",
            {"cycle_id": cycle_id, "severity": {"$in": ["critical", "error"]}},
            limit=2000,
        ) or []
        for row in logs:
            if not isinstance(row, dict):
                continue
            message = str(row.get("message") or "")
            kind = _classify_recovery(message, _RECOVERY_CLASSES)
            if not kind:
                continue
            by_type[kind] += 1
            agent = _agent_in(message)
            if agent:
                by_agent[agent] += 1
            events.append({
                "at": row.get("timestamp"),
                "type": kind,
                "agent": agent,
                "detail": message[:200],
            })

        pipe = mongo_store.find_docs(
            "pipeline_events", {"cycle_id": cycle_id}, limit=5000
        ) or []
        for row in pipe:
            if not isinstance(row, dict):
                continue
            step = str(row.get("step") or "")
            kind = _classify_recovery(step, _RECOVERY_STEPS)
            if not kind:
                continue
            # An agent crash is logged BOTH as an audit row and as an event;
            # count the event only for classes the log does not carry, so the
            # total is a count of incidents rather than of rows.
            if kind in ("agent_crashed", "agent_cancelled"):
                continue
            by_type[kind] += 1
            detail = str(row.get("detail") or "")
            agent = _agent_in(step) or _agent_in(detail)
            if agent:
                by_agent[agent] += 1
            events.append({
                "at": row.get("timestamp"),
                "type": kind,
                "agent": agent,
                "detail": detail[:200] or step[:200],
            })
    except Exception as e:  # noqa: BLE001 — a report must survive a store blip
        logger.warning(
            "[AR] recovery audit could not read the store for %s: %s", cycle_id, e
        )
        return {
            **stats,
            "total_failures": None,
            "error": f"{type(e).__name__}: {e}",
        }

    events.sort(key=lambda e: (e.get("at") is None, e.get("at")))
    # The store hands back datetimes; core.py writes this dict with json.dumps.
    # Sorting above needs the datetime, the writer needs a string — convert
    # AFTER the sort. The first live run of this function (cycle-v3-1788660665)
    # took the whole autoresearch report to `error` on exactly this TypeError.
    for e in events:
        at = e.get("at")
        if hasattr(at, "isoformat"):
            e["at"] = at.isoformat()
    stats["by_type"] = dict(by_type)
    stats["by_agent"] = dict(by_agent)
    stats["total_failures"] = sum(by_type.values())
    stats["resilience_exhausted"] = by_type.get("resilience_exhausted", 0)
    stats["circuit_breakers_tripped"] = by_type.get("circuit_breaker_tripped", 0)
    stats["recent_events"] = events[-10:]
    return stats

def _audit_execution_errors(cycle_id: str) -> list[dict]:
    try:
        from app.db import mongo_store
        if mongo_store.reads_mongo("execution_errors"):
            try:
                docs = mongo_store.find_docs(
                    "execution_errors",
                    {"cycle_id": cycle_id},
                    sort=[("created_at", -1)],
                    limit=5,
                    projection={"phase": 1, "error_type": 1, "error_message": 1},
                )
                return [{"phase": d.get("phase"), "error_type": d.get("error_type"), "error_message": d.get("error_message")} for d in docs]
            except Exception as me:
                mongo_store.handle_mongo_read_failure("execution_errors", "_audit_execution_errors", me)

        rows = mongo_query.find_rows('execution_errors', {'cycle_id': cycle_id}, ['phase', 'error_type', 'error_message'], sort=[('created_at', -1)], limit=5)
        return [{"phase": r[0], "error_type": r[1], "error_message": r[2]} for r in rows]
    except Exception as e:
        logger.debug("Failed to fetch execution errors: %s", e)
    return []
