import logging

from app.db import mongo_query
from app.autoresearch.trace_evidence import trace_quality_window
from app.v3.shared_desk import OutcomeClass, outcomes_in

LLM_SCORE_VERSION = "llm_execution_time_v2"
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Terminal outcomes that mean the agent run itself failed. A row whose outcome
# was downgraded (e.g. a second artifact failure becomes DATA_GAP so the desk
# is not aborted) still carries its failure_reason — count those too, or the
# downgrade hides the failure from this audit exactly the way it hides it from
# _check_abort.
#
# Derived from the shared taxonomy, not re-listed: a private tuple here is
# what let a CANCELLED row be scored as an LLM failure while the replay router
# drew the same row as a benign "other".
_FAILED_OUTCOMES = outcomes_in(OutcomeClass.FAILED)

#: Outcomes that must not be SCORED at all — neither numerator nor
#: denominator. A cancellation is the operator stopping the run (deploying
#: this service SIGTERMs in-flight cycles, so cancels cluster around deploys),
#: not a model failing to answer, and it always carries failure_reason
#: CANCELLED — so the `failure_reason IS NOT NULL` branch below swept every
#: one of them into the failure count. With availability scored 1 - 2*fail
#: rate, a cycle an operator killed with their own deploy could drive LLM
#: availability to 0 and raise a false "LLM unhealthy".
#:
#: Excluding them from `failed` alone would be the same bug with the sign
#: flipped — the surviving denominator would make availability look better
#: than the evidence supports — so they leave BOTH counts and are reported
#: separately as `cancelled_calls`.
_UNSCORED_OUTCOMES = outcomes_in(OutcomeClass.ABANDONED)

#: The whole permitted vocabulary. Anything else in the column is a value no
#: reader here understands; it is counted and NAMED rather than bucketed.
_KNOWN_OUTCOMES = outcomes_in()


def _audit_llm_traces(cycle_id: str) -> dict:
    """Score LLM performance = availability + output quality, not just uptime.

    Availability is measured from THIS cycle's `v3_agent_telemetry` rows (one
    per agent attempt, cycle_id-keyed). It used to read the in-process
    LLMTracker singleton, which lost its only writer in the fa7cee3 SDK
    migration (2026-06-25) and read `total_calls=0` on every cycle forever —
    and 0 calls scored as availability 1.0, so the audit reported a perfect
    LLM on zero evidence while telling the reflection LLM "LLM calls: 0".
    (The audit also runs in the eval worker, a different process from the
    cycle, so an in-memory counter was the wrong instrument even when wired.)

    Blend:
      - availability (0.5): 1 - 2*fail_rate over this cycle's SCORABLE agent
        runs; None (not 1.0) when the cycle has no telemetry rows at all.
        Cancelled runs (OutcomeClass.ABANDONED) are excluded from BOTH the
        numerator and the denominator — an operator stopping a cycle, which a
        deploy does to every in-flight run, is not the model failing to
        answer. They are reported as `cancelled_calls` instead of scored.
      - judge quality (0.3): decision_evaluations.final_quality_score (0-5,
        LLM-as-judge over real decisions), 7d average
      - eval quality (0.2): scored tool traces executed in the last 7d
        (grading time cannot make an old model call recent)
    The score is the weighted average renormalized over the components that
    have evidence; a component with no evidence contributes nothing rather
    than borrowing another component's number. No evidence anywhere -> 0.5
    ("could not measure") plus a named issue — never a silent perfect score.
    """
    issues = []
    try:
        cycle_filter = {"cycle_id": cycle_id}
        # Every scored count runs over the SCORABLE rows only, so the
        # numerator and the denominator can never disagree about what is in
        # the population.
        scored_filter = {
            "cycle_id": cycle_id,
            "outcome": {"$nin": list(_UNSCORED_OUTCOMES)},
        }
        total_calls = mongo_query.count("v3_agent_telemetry", scored_filter)
        failed = mongo_query.count("v3_agent_telemetry", {
            **scored_filter,
            "$or": [
                {"outcome": {"$in": list(_FAILED_OUTCOMES)}},
                {"failure_reason": {"$ne": None}},
            ],
        })
        # Excluded from the score, never from the operator's view.
        cancelled_calls = mongo_query.count("v3_agent_telemetry", {
            "cycle_id": cycle_id,
            "outcome": {"$in": list(_UNSCORED_OUTCOMES)},
        })
        unrecognised_calls = mongo_query.count("v3_agent_telemetry", {
            "cycle_id": cycle_id,
            "outcome": {"$nin": list(_KNOWN_OUTCOMES)},
        })
        # Context only: the per-decision LLM ledger (~1 row per cycle since
        # the per-call logging died). It cannot express failure, so it never
        # feeds fail_rate — but "0 telemetry AND 0 ledger rows" is the
        # difference between "cycle made no calls" and "telemetry is broken".
        llm_calls_logged = mongo_query.count("llm_audit_logs", cycle_filter)

        if total_calls > 0:
            fail_rate = failed / total_calls
            availability = max(0.0, 1.0 - fail_rate * 2)
        elif cancelled_calls:
            # Every run in the cycle was stopped from outside. That is not a
            # failing LLM and it is not missing telemetry either — naming the
            # cancellation is the difference between "the operator killed it"
            # and "the instrument is broken".
            fail_rate = None
            availability = None
            issues.append({
                "issue": (
                    f"Cycle {cycle_id} was stopped before any agent run could "
                    f"be scored: all {cancelled_calls} telemetry rows are "
                    f"cancellations (a deploy or an operator stop) — "
                    f"availability unmeasured, not zero"
                ),
                "severity": "info",
            })
        else:
            # No evidence is not health. Refuse to compute, and say so.
            fail_rate = None
            availability = None
            issues.append({
                "issue": (
                    f"No per-cycle LLM activity evidence for {cycle_id}: "
                    f"0 rows in v3_agent_telemetry, {llm_calls_logged} in "
                    f"llm_audit_logs — availability unmeasured"
                ),
                "severity": "warning",
            })

        if cancelled_calls and total_calls > 0:
            issues.append({
                "issue": (
                    f"{cancelled_calls} agent run(s) in {cycle_id} were "
                    f"cancelled (operator stop / deploy) and are excluded "
                    f"from availability — {total_calls} scorable run(s) remain"
                ),
                "severity": "info",
            })
        if unrecognised_calls:
            issues.append({
                "issue": (
                    f"{unrecognised_calls} telemetry row(s) in {cycle_id} "
                    f"carry an outcome outside the known vocabulary "
                    f"{list(_KNOWN_OUTCOMES)} — classify it in "
                    f"app/v3/shared_desk.OutcomeClass before trusting this "
                    f"cycle's availability"
                ),
                "severity": "warning",
            })

        judge_avg = None
        eval_avg = None
        deepeval_dead = False
        tool_evidence = {}
        try:
            row = mongo_query.agg_row('decision_evaluations', {'timestamp': {'$gt': (datetime.now(timezone.utc) - timedelta(days=7))}, 'final_quality_score': {'$ne': None}}, [('avg', 'final_quality_score'), ('count', None)])
            if row and row[1] and row[1] >= 3:
                judge_avg = max(0.0, min(1.0, float(row[0]) / 5.0))

            # "Dead" must mean dead NOW — judge over the newest rows only.
            # A 7-day window kept flagging for a week after the grounding
            # judge was fixed, because the pre-fix error rows dominated.
            # The FILTER/subquery pair counted, over the NEWEST 10 rows in
            # the 7-day window, how many carry a deepeval_error. The
            # LIMIT-then-aggregate order is the point of the subquery, so it
            # is preserved here: fetch the 10 newest, count in Python.
            recent = mongo_query.find_rows(
                'decision_evaluations',
                {'timestamp': {'$gt': (datetime.now(timezone.utc) - timedelta(days=7))}},
                ['evidence_gathering'],
                sort=[('timestamp', -1)], limit=10,
            )
            de_total = len(recent)
            de_errors = sum(1 for (eg,) in recent if 'deepeval_error' in str(eg))
            if de_total >= 3 and de_errors > de_total * 0.5:
                deepeval_dead = True

            tool_evidence = trace_quality_window()
            if tool_evidence['scored_count'] >= 10 and tool_evidence['mean_score'] is not None:
                eval_avg = max(0.0, min(1.0, tool_evidence['mean_score'] / 100.0))
            if tool_evidence['pending_count']:
                issues.append({'issue':f"Tool grading incomplete: {tool_evidence['pending_count']} of {tool_evidence['trace_count']} recent calls await grading", 'severity':'warning'})
        except Exception as q_err:
            logger.debug("[LLM-AUDIT] Quality component lookup skipped: %s", q_err)

        # Weighted average over the components that actually have evidence.
        components = [
            (0.5, availability),
            (0.3, judge_avg),
            (0.2, eval_avg),
        ]
        live = [(w, v) for w, v in components if v is not None]
        if live:
            weight_sum = sum(w for w, _ in live)
            current_score = sum(w * v for w, v in live) / weight_sum
        else:
            current_score = 0.5
            issues.append({
                "issue": (
                    "No LLM evidence at all (no cycle telemetry, no 7d judge "
                    "or eval rows) — score is the could-not-measure default"
                ),
                "severity": "warning",
            })

        # Trend drift vs prior reports (sourced from autoresearch_reports —
        # the old subsystem_benchmarks module was deleted in the V3 purge).
        history_scores = []
        try:
            rows = mongo_query.find_rows('autoresearch_reports', {'llm_performance_score': {'$ne': None}, 'llm_score_version':LLM_SCORE_VERSION}, ['llm_performance_score'], sort=[('created_at', -1)], limit=10)
            history_scores = [float(r[0]) / 100.0 for r in rows if r[0] is not None]
        except Exception as trend_err:
            logger.debug("[LLM-AUDIT] Trend lookup skipped: %s", trend_err)

        if len(history_scores) >= 3:
            avg_score = sum(history_scores) / len(history_scores)
            if current_score < avg_score - 0.15:
                issues.append({
                    "issue": f"LLM performance has degraded (current: {current_score:.0%} vs rolling historical average: {avg_score:.0%})",
                    "severity": "warning"
                })
        else:
            avg_score = current_score

        if fail_rate is not None and fail_rate > 0.1:
            issues.append({"issue": f"LLM failure rate: {fail_rate:.0%} ({failed}/{total_calls} agent runs this cycle)", "severity": "warning"})
        if judge_avg is not None and judge_avg < 0.6:
            issues.append({"issue": f"LLM-judge decision quality low: {judge_avg:.0%} (7d avg)", "severity": "warning"})
        if deepeval_dead:
            issues.append({
                "issue": "Judge grounding metrics (faithfulness/relevancy) are dead — deepeval infra errors on most evaluations",
                "severity": "warning",
            })

        return {
            "score": round(current_score, 3),
            "score_version": LLM_SCORE_VERSION,
            "score_components": {"cycle_agent_success":availability, "historical_judge_7d":judge_avg,
                                 "historical_tool_execution_7d":eval_avg},
            "tool_evidence": tool_evidence,
            "total_calls": total_calls,
            "failed_calls": failed,
            "cancelled_calls": cancelled_calls,
            "unrecognised_calls": unrecognised_calls,
            "llm_calls_logged": llm_calls_logged,
            "source": "v3_agent_telemetry",
            "fail_rate": round(fail_rate, 3) if fail_rate is not None else None,
            "availability": round(availability, 3) if availability is not None else None,
            "judge_quality_7d": round(judge_avg, 3) if judge_avg is not None else None,
            "eval_quality_7d": round(eval_avg, 3) if eval_avg is not None else None,
            "historical_average": round(avg_score, 3),
            "issues": issues,
        }
    except Exception as e:
        logger.warning("[LLM-AUDIT] Failed to audit traces: %s", e)
        return {
            "score": 0.5,
            "issues": [{
                "issue": f"LLM audit itself failed ({type(e).__name__}: {e}) — score is the could-not-measure default",
                "severity": "warning",
            }],
        }
