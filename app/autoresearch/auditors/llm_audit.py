import logging

from app.db import mongo_query
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Terminal outcomes that mean the agent run itself failed. A row whose outcome
# was downgraded (e.g. a second artifact failure becomes DATA_GAP so the desk
# is not aborted) still carries its failure_reason — count those too, or the
# downgrade hides the failure from this audit exactly the way it hides it from
# _check_abort.
_FAILED_OUTCOMES = ("AGENT_ERROR", "TIMED_OUT")


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
      - availability (0.5): 1 - 2*fail_rate over this cycle's agent runs;
        None (not 1.0) when the cycle has no telemetry rows at all
      - judge quality (0.3): decision_evaluations.final_quality_score (0-5,
        LLM-as-judge over real decisions), 7d average
      - eval quality (0.2): eval_scores.final_score (0-100 trace evals), 7d avg
    The score is the weighted average renormalized over the components that
    have evidence; a component with no evidence contributes nothing rather
    than borrowing another component's number. No evidence anywhere -> 0.5
    ("could not measure") plus a named issue — never a silent perfect score.
    """
    issues = []
    try:
        cycle_filter = {"cycle_id": cycle_id}
        total_calls = mongo_query.count("v3_agent_telemetry", cycle_filter)
        failed = mongo_query.count("v3_agent_telemetry", {
            "cycle_id": cycle_id,
            "$or": [
                {"outcome": {"$in": list(_FAILED_OUTCOMES)}},
                {"failure_reason": {"$ne": None}},
            ],
        })
        # Context only: the per-decision LLM ledger (~1 row per cycle since
        # the per-call logging died). It cannot express failure, so it never
        # feeds fail_rate — but "0 telemetry AND 0 ledger rows" is the
        # difference between "cycle made no calls" and "telemetry is broken".
        llm_calls_logged = mongo_query.count("llm_audit_logs", cycle_filter)

        if total_calls > 0:
            fail_rate = failed / total_calls
            availability = max(0.0, 1.0 - fail_rate * 2)
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

        judge_avg = None
        eval_avg = None
        deepeval_dead = False
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

            ev = mongo_query.agg_row('eval_scores', {'created_at': {'$gt': (datetime.now(timezone.utc) - timedelta(days=7))}, 'final_score': {'$ne': None}}, [('avg', 'final_score'), ('count', None)])
            if ev and ev[1] and ev[1] >= 10:
                eval_avg = max(0.0, min(1.0, float(ev[0]) / 100.0))
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
            rows = mongo_query.find_rows('autoresearch_reports', {'llm_performance_score': {'$ne': None}}, ['llm_performance_score'], sort=[('created_at', -1)], limit=10)
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
            "total_calls": total_calls,
            "failed_calls": failed,
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
