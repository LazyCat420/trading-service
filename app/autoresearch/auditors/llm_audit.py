import logging

from app.db import mongo_query
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _audit_llm_traces(cycle_id: str) -> dict:
    """Score LLM performance = availability + output quality, not just uptime.

    The old formula was fail_rate only, which pinned the score at 100 whenever
    every call merely *returned* — truncated, ungrounded, or low-quality output
    scored the same as excellent output. Blend:
      - availability (0.5): 1 - 2*fail_rate from the in-process tracker
      - judge quality (0.3): decision_evaluations.final_quality_score (0-5,
        LLM-as-judge over real decisions), 7d average
      - eval quality (0.2): eval_scores.final_score (0-100 trace evals), 7d avg
    Quality components fall back to availability when their tables are thin,
    so cold starts don't punish or inflate.
    """
    issues = []
    try:
        from app.monitoring.llm_tracker import tracker
        stats = tracker.get_stats()
        total_calls = stats.get("total_calls", 0)
        failed = stats.get("failed_calls", 0)
        fail_rate = (failed / total_calls) if total_calls else 0.0
        availability = max(0.0, 1.0 - fail_rate * 2)

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

        current_score = (
            0.5 * availability
            + 0.3 * (judge_avg if judge_avg is not None else availability)
            + 0.2 * (eval_avg if eval_avg is not None else availability)
        )

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

        if fail_rate > 0.1:
            issues.append({"issue": f"LLM failure rate: {fail_rate:.0%}", "severity": "warning"})
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
            "fail_rate": round(fail_rate, 3),
            "availability": round(availability, 3),
            "judge_quality_7d": round(judge_avg, 3) if judge_avg is not None else None,
            "eval_quality_7d": round(eval_avg, 3) if eval_avg is not None else None,
            "historical_average": round(avg_score, 3),
            "issues": issues,
        }
    except Exception as e:
        logger.warning("[LLM-AUDIT] Failed to audit traces: %s", e)
        return {"score": 0.5, "issues": []}
