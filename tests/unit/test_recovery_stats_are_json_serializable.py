"""The first live run of recovery_stats took down the autoresearch report.

MEASURED 2026-09-06, cycle-v3-1788660665 — the first cycle after 56356ec
("recovery_stats has never recorded a failure, on any cycle"). The report row:

    status=error  phase=error
    error="Object of type datetime is not JSON serializable"

Traceback: `core.py:272` writes the report with `'recovery_stats':
json.dumps(recovery)`, and `_audit_recovery` had just started returning
`recent_events` whose `"at"` is the cycle_audit_log row's raw BSON datetime.
Three minutes of audit work, then the whole report went to `error` — data
quality, decision quality and LLM scores included — because one new field
carried a type the writer cannot encode.

56356ec's own tests built rows WITH datetimes (`_log(..., ts=datetime(...))`)
and asserted counts, types and ordering on the returned dict. None of them
handed the dict to the writer's serializer, so the test could not see the
failure the writer would raise. A test that proves the shape of a result must
also prove the result survives the one operation every consumer performs on it.
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

from app.autoresearch.auditors.performance_audit import _audit_recovery

CYCLE = "cycle-v3-1788660665"

# Verbatim classes from the failing cycle's cycle_audit_log, timestamps as the
# store returns them: datetime objects, not strings.
STALL = {
    "cycle_id": CYCLE, "severity": "error",
    "message": "[ERROR] Prism stream error: Provider stream stalled: no data received for 300s",
    "timestamp": datetime(2026, 9, 6, 2, 58, 12),
}
TIMEOUT = {
    "cycle_id": CYCLE, "severity": "error",
    "message": "[ERROR] [V3Runner] v3_fundamental_analyst TIMEOUT for ABT after 1800068ms",
    "timestamp": datetime(2026, 9, 6, 3, 3, 45),
}


def _run(logs, events=()):
    def find_docs(collection, query=None, **kwargs):
        return list(logs) if collection == "cycle_audit_log" else (list(events) if collection == "pipeline_events" else [])
    with patch("app.autoresearch.auditors.performance_audit.mongo_store") as ms:
        ms.find_docs.side_effect = find_docs
        return _audit_recovery(CYCLE)


def test_the_stats_survive_the_writers_serializer():
    """`core.py` does `json.dumps(recovery)`. So does this test."""
    stats = _run([STALL, TIMEOUT])
    assert stats["total_failures"] >= 1, stats
    json.dumps(stats)  # raised TypeError on the live cycle


def test_the_events_are_still_ordered_by_time_after_conversion():
    stats = _run([TIMEOUT, STALL])  # out of order on purpose
    ats = [e["at"] for e in stats["recent_events"]]
    assert ats == sorted(ats), ats
    assert all(isinstance(a, str) for a in ats), ats


def test_a_row_with_no_timestamp_still_serializes_and_sorts_last():
    stats = _run([dict(STALL, timestamp=None), TIMEOUT])
    json.dumps(stats)
    assert stats["recent_events"][-1]["at"] is None
