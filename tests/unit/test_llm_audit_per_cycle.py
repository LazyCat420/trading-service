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
from app.v3.shared_desk import OutcomeClass, outcomes_in


class _FakeMongoQuery:
    """Route by collection; record every filter handed to count()."""

    def __init__(self, counts=None, agg=None, rows=None):
        self.counts = counts or {}
        self.agg = agg or {}
        self.rows = rows or {}
        self.count_calls: list[tuple[str, dict]] = []

    def count(self, collection, query=None):
        self.count_calls.append((collection, query or {}))
        if collection != "v3_agent_telemetry":
            return self.counts.get(collection, 0)
        # Four counts hit v3_agent_telemetry, told apart by query SHAPE:
        #   $or                    -> failed runs
        #   outcome $in            -> the unscored (cancelled) census
        #   outcome $nin SUCCESS.. -> the unknown-vocabulary census; only the
        #                             whole-vocabulary filter can contain
        #                             SUCCESS, the scored filter excludes
        #                             abandoned outcomes only
        #   anything else          -> the scored total
        q = query or {}
        if "$or" in q:
            return self.counts.get("v3_agent_telemetry_failed", 0)
        cond = q.get("outcome") or {}
        if "$in" in cond:
            return self.counts.get("v3_agent_telemetry_cancelled", 0)
        if "SUCCESS" in set(cond.get("$nin") or ()):
            return self.counts.get("v3_agent_telemetry_unrecognised", 0)
        return self.counts.get("v3_agent_telemetry", 0)

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
    with patch.object(llm_audit, "mongo_query", fake), \
         patch.object(llm_audit, "trace_quality_window", return_value={"scored_count":0,"trace_count":0,"pending_count":0,"mean_score":None}):
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
    # Derived from the shared taxonomy in both directions, never from a
    # literal: a private copy of the list here is exactly what let CANCELLED
    # mean "LLM failure" in this file and "benign other" in the replay router.
    assert set(outcome_branch["outcome"]["$in"]) == set(outcomes_in(OutcomeClass.FAILED))
    assert "CANCELLED" not in set(outcome_branch["outcome"]["$in"]), (
        "an operator stopping the run is not the model failing to answer")


# ── Cancellation is an EXCLUSION, not a failure and not a free pass ────
#
# Deploying this service SIGTERMs in-flight cycles, so cancellations cluster
# around deploys. Every cancelled row carries failure_reason=CANCELLED, so the
# `failure_reason IS NOT NULL` branch above counted all of them as LLM
# failures — and availability is 1 - 2*fail_rate, so an operator's own deploy
# could drive the LLM score to 0 and raise "LLM unhealthy". These tests fail
# on the pre-fix code, which scored total=10 / failed=4 / availability=0.2 for
# the first case below.


def _matches(doc: dict, query: dict) -> bool:
    """The Mongo operators this audit actually issues, evaluated in Python.

    Missing-field semantics follow Mongo: `$nin` MATCHES a doc with no such
    field, and `{"$ne": None}` does NOT.
    """
    for key, cond in query.items():
        if key == "$or":
            if not any(_matches(doc, branch) for branch in cond):
                return False
            continue
        val = doc.get(key)
        if isinstance(cond, dict):
            for op, arg in cond.items():
                if op == "$in" and val not in arg:
                    return False
                if op == "$nin" and val in arg:
                    return False
                if op == "$ne" and val == arg:
                    return False
        elif val != cond:
            return False
    return True


class _TelemetryRows(_FakeMongoQuery):
    """A fake that holds real telemetry ROWS and evaluates the filters.

    A count-keyed fake cannot see the bug these tests are for: dropping
    cancelled rows from the failure numerator while leaving them in the
    denominator is the SAME defect with the sign flipped, and only a fake that
    applies both filters to one population can tell the two apart.
    """

    def __init__(self, rows_, **kw):
        super().__init__(**kw)
        self.telemetry = rows_

    def count(self, collection, query=None):
        self.count_calls.append((collection, query or {}))
        if collection != "v3_agent_telemetry":
            return self.counts.get(collection, 0)
        return sum(1 for d in self.telemetry if _matches(d, query or {}))


def _row(outcome, *, cycle_id="cycle-test-1", failure_reason=None):
    row = {"cycle_id": cycle_id, "outcome": outcome}
    if failure_reason is not None:
        row["failure_reason"] = failure_reason
    return row


def _cancelled(n, cycle_id="cycle-test-1"):
    # The shape agent_runner writes: outcome CANCELLED, and failure_reason
    # ALWAYS set (output_rules.CANCELLED), which is what reached the numerator.
    return [_row("CANCELLED", cycle_id=cycle_id, failure_reason="CANCELLED")
            for _ in range(n)]


def test_a_cancelled_run_cannot_manufacture_an_unhealthy_llm():
    fake = _TelemetryRows([_row("SUCCESS")] * 6 + _cancelled(4))
    out = _audit_with(fake)
    assert out["total_calls"] == 6, (
        "cancelled runs must leave the denominator as well as the numerator")
    assert out["failed_calls"] == 0, (
        "no model failed here — an operator (or a deploy's SIGTERM) stopped "
        "four runs; pre-fix this read failed=4 through the failure_reason arm")
    assert out["fail_rate"] == 0.0
    assert out["availability"] == 1.0
    assert out["cancelled_calls"] == 4, "the cancellation must stay VISIBLE"
    assert any("cancel" in i["issue"].lower() for i in out["issues"]), out["issues"]


def test_excluding_cancels_from_the_numerator_alone_would_flatter_the_score():
    """The sign-flipped version of the same bug.

    5 success, 5 cancelled, 1 real AGENT_ERROR. Scored over the 6 runs that
    could be scored: fail_rate 1/6, availability 0.667. Leaving the cancels in
    the denominator would report 1/11 and availability 0.818 — a real failure
    diluted by runs that were never attempts.
    """
    fake = _TelemetryRows(
        [_row("SUCCESS")] * 5
        + _cancelled(5)
        + [_row("AGENT_ERROR", failure_reason="RUNNER_EXCEPTION")]
    )
    out = _audit_with(fake)
    assert out["total_calls"] == 6
    assert out["failed_calls"] == 1
    assert out["availability"] == round(1.0 - 2 * (1 / 6), 3)
    assert out["availability"] < 0.818, (
        "the one real failure must not be diluted by the five cancelled runs")


def test_a_wholly_cancelled_cycle_is_unmeasured_not_zero():
    out = _audit_with(_TelemetryRows(_cancelled(8)))
    assert out["total_calls"] == 0
    assert out["availability"] is None, (
        "a cycle a deploy killed outright has no availability evidence — "
        "scoring it 0 is how the operator's own restart reads as an outage")
    assert out["cancelled_calls"] == 8
    issue = next((i for i in out["issues"] if "cancel" in i["issue"].lower()), None)
    assert issue is not None, out["issues"]
    assert issue["severity"] == "info", (
        "the operator stopping a cycle is not a warning about the LLM")


def test_an_outcome_nobody_classified_is_reported_not_bucketed():
    """The permitted set must make the NEXT new value loud.

    SKIPPED is the historical example (one dormant row since August). It is
    not in the vocabulary, so it must be counted and NAMED — not absorbed by
    whichever branch happens to be last.
    """
    fake = _TelemetryRows([_row("SUCCESS")] * 3 + [_row("SKIPPED")])
    out = _audit_with(fake)
    assert out["unrecognised_calls"] == 1
    assert any("vocabulary" in i["issue"].lower() and i["severity"] == "warning"
               for i in out["issues"]), out["issues"]
    assert "SKIPPED" not in set(outcomes_in()), (
        "if SKIPPED is ever classified, this test should assert the new "
        "bucket rather than the warning")


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


# ── The number must not lose the cancellation on its way downstream ────
#
# `total_calls` is now the SCORABLE population. A reader that prints it
# unqualified says "6 runs, 0 failed" for a cycle that actually attempted ten
# and had four killed by a deploy — or, worse, "unmeasured (no telemetry
# rows)" for a cycle whose telemetry is complete and entirely cancellations.
# That is the projection lying about its own row, so both readers of this
# dict are pinned here.

def _reflection_prompt(llm_analysis: dict) -> str:
    import asyncio
    from unittest.mock import AsyncMock

    import app.autoresearch.reflection as reflection
    import app.services.prism_agent_caller as pac

    captured = {}

    async def _chat(**kwargs):
        captured["user"] = kwargs["user"]
        return ('{"summary": "x", "recommendations": [], "urgent_data_gaps": [], '
                '"system_health": "healthy", "schedule_recommendation": null}'), 10, 0.1

    with patch.object(pac.llm, "chat", AsyncMock(side_effect=_chat)), \
         patch.object(reflection, "_recall_past_lessons", lambda _b: ""):
        asyncio.run(reflection._reflect({"llm_analysis": llm_analysis}))
    return captured["user"]


def test_the_reflection_prompt_names_the_cancelled_runs_it_excluded():
    prompt = _reflection_prompt({
        "total_calls": 6, "failed_calls": 0, "cancelled_calls": 4,
        "availability": 1.0,
    })
    line = next(ln for ln in prompt.splitlines() if ln.startswith("Agent LLM runs"))
    assert "6" in line and "failed runs: 0" in line
    assert "cancelled" in line.lower() and "4" in line, (
        "6/0 with no mention of the 4 stopped runs is the same projection "
        "defect as reading total_calls off a filtered population: the line "
        "is true about the numbers and false about the cycle")


def test_a_wholly_cancelled_cycle_is_not_reported_as_missing_telemetry():
    prompt = _reflection_prompt({
        "total_calls": 0, "failed_calls": 0, "cancelled_calls": 8,
        "availability": None,
    })
    line = next(ln for ln in prompt.splitlines() if ln.startswith("Agent LLM runs"))
    assert "no telemetry rows" not in line, (
        "the telemetry is complete — every row is a cancellation. Telling the "
        "reflection LLM the instrument is empty is how it concluded, on a "
        "healthy cycle, that the decision engine never ran")
    assert "CANCELLED" in line and "8" in line


def test_an_ordinary_cycle_keeps_its_original_line():
    prompt = _reflection_prompt({"total_calls": 12, "failed_calls": 2,
                                 "cancelled_calls": 0, "availability": 0.667})
    line = next(ln for ln in prompt.splitlines() if ln.startswith("Agent LLM runs"))
    assert line == "Agent LLM runs this cycle: 12, failed runs: 2"
