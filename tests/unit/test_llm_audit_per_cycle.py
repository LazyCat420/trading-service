"""_audit_llm_traces must measure THE CYCLE IT WAS ASKED ABOUT.

For six weeks it read the in-process LLMTracker singleton, whose only writer
was deleted in the fa7cee3 SDK migration (2026-06-25). total_calls was
structurally 0 on every cycle forever, cycle_id was accepted and ignored, and
0 calls scored availability = 1.0 — so the audit told the reflection LLM
"LLM calls: 0" while granting a perfect availability score, and the reflection
concluded (on a healthy cycle, ar-82d9f0417478) that the decision engine never
ran. Each test here fails on that pre-fix code.
"""

import inspect
from unittest.mock import patch

import app.autoresearch.auditors.llm_audit as llm_audit
from app.autoresearch.auditors.llm_audit import _audit_llm_traces


class _FakeMongoQuery:
    """Route by collection; record every filter handed to count()."""

    def __init__(self, counts=None, agg=None, rows=None):
        self.counts = counts or {}
        self.agg = agg or {}
        self.rows = rows or {}
        self.count_calls: list[tuple[str, dict]] = []

    def count(self, collection, query=None):
        self.count_calls.append((collection, query or {}))
        val = self.counts.get(collection, 0)
        # Two counts hit v3_agent_telemetry: total (no $or) and failed ($or).
        if collection == "v3_agent_telemetry" and query and "$or" in query:
            return self.counts.get("v3_agent_telemetry_failed", 0)
        return val

    def agg_row(self, collection, query, aggs, session=None):
        return self.agg.get(collection,
                            tuple(0 if op.startswith("count") else None
                                  for op, _ in aggs))

    def find_rows(self, collection, query, columns, sort=None, limit=0,
                  session=None):
        return self.rows.get(collection, [])

    def find_dicts(self, *a, **k):
        return []


def _audit_with(fake, cycle_id="cycle-test-1"):
    with patch.object(llm_audit, "mongo_query", fake):
        return _audit_llm_traces(cycle_id)


def test_per_cycle_failures_reach_the_fail_rate():
    fake = _FakeMongoQuery(counts={
        "v3_agent_telemetry": 10,
        "v3_agent_telemetry_failed": 3,
        "llm_audit_logs": 1,
    })
    out = _audit_with(fake)
    assert out["total_calls"] == 10
    assert out["failed_calls"] == 3
    assert out["fail_rate"] == 0.3
    assert out["availability"] == 0.4  # 1 - 2*0.3


def test_no_evidence_never_reads_as_perfect_availability():
    out = _audit_with(_FakeMongoQuery())
    assert out["availability"] is None, (
        "0 telemetry rows must be 'unmeasured', never availability=1.0 — "
        "that default is how a dead counter scored a perfect LLM for weeks")
    assert out["fail_rate"] is None
    assert any("evidence" in i["issue"].lower() or "unmeasured" in i["issue"].lower()
               for i in out["issues"]), out["issues"]


def test_the_cycle_id_is_actually_used():
    fake = _FakeMongoQuery(counts={"v3_agent_telemetry": 2})
    _audit_with(fake, cycle_id="cycle-xyz-42")
    telemetry_filters = [q for c, q in fake.count_calls
                        if c == "v3_agent_telemetry"]
    assert telemetry_filters, "no per-cycle telemetry query was issued at all"
    assert all(q.get("cycle_id") == "cycle-xyz-42" for q in telemetry_filters), (
        "the audit accepted a cycle_id but did not scope its queries to it")


def test_the_dead_tracker_is_not_consulted():
    # Poison the tracker: if the audit still reads it, this leaks through.
    import app.monitoring.llm_tracker as lt
    fake = _FakeMongoQuery(counts={"v3_agent_telemetry": 5})
    with patch.object(lt.tracker, "get_stats",
                      lambda: {"total_calls": 10**9, "failed_calls": 10**9}):
        out = _audit_with(fake)
    assert out["total_calls"] == 5
    assert "llm_tracker" not in inspect.getsource(llm_audit), (
        "the dead in-memory tracker must not be imported here — its writer "
        "was deleted in fa7cee3 and it reads 0 in every process")


def test_downgraded_retry_failures_count_as_failures():
    """The failed-runs filter must catch failure_reason-bearing rows.

    agent_runner downgrades a second artifact failure to DATA_GAP (so
    _check_abort spares the desk) but still stamps failure_reason. A filter
    on outcome alone reads those runs as healthy data gaps.
    """
    fake = _FakeMongoQuery(counts={"v3_agent_telemetry": 3,
                                   "v3_agent_telemetry_failed": 1})
    _audit_with(fake)
    or_filters = [q["$or"] for c, q in fake.count_calls
                  if c == "v3_agent_telemetry" and "$or" in q]
    assert or_filters, "no failed-runs query was issued"
    branches = or_filters[0]
    assert {"failure_reason": {"$ne": None}} in branches, (
        "failure_reason IS NOT NULL must be a failure branch, or downgraded "
        "retry failures (the judge-fails-twice shape) vanish from fail_rate")
    outcome_branch = next((b for b in branches if "outcome" in b), None)
    assert outcome_branch is not None
    assert set(outcome_branch["outcome"]["$in"]) == {"AGENT_ERROR", "TIMED_OUT"}


def test_score_renormalizes_without_availability():
    # No cycle telemetry, but 7d judge evidence exists (avg 4.0/5 over 5 rows).
    fake = _FakeMongoQuery(agg={"decision_evaluations": (4.0, 5)})
    out = _audit_with(fake)
    assert out["availability"] is None
    assert out["judge_quality_7d"] == 0.8
    assert out["score"] == 0.8, (
        "with availability unmeasured the score must renormalize over the "
        "components that exist — not let a fabricated availability=1.0 "
        "contribute half the weight")
