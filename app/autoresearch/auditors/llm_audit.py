import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def _audit_llm_traces(cycle_id: str) -> dict:
    issues = []
    try:
        from app.monitoring.llm_tracker import tracker
        stats = tracker.get_stats()
        total_calls = stats.get("total_calls", 0)
        failed = stats.get("failed_calls", 0)
        if total_calls == 0:
            current_score = 1.0
            fail_rate = 0.0
        else:
            fail_rate = failed / total_calls
            current_score = max(0.0, 1.0 - fail_rate * 2)

        # Query historical trends to detect drift. Sourced from prior
        # autoresearch_reports rows — the old app.pipeline.subsystem_benchmarks
        # module was deleted in the V3 purge and its import silently forced
        # this whole audit onto the 0.5-default score every cycle.
        trends = []
        try:
            from app.db.connection import get_db
            with get_db() as db:
                rows = db.execute(
                    "SELECT llm_performance_score FROM autoresearch_reports "
                    "WHERE llm_performance_score IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 10"
                ).fetchall()
            trends = [{"metrics": {"llm_performance_score": r[0]}} for r in rows]
        except Exception as trend_err:
            logger.debug("[LLM-AUDIT] Trend lookup skipped: %s", trend_err)

        history_scores = []
        for t in trends:
            metrics = t.get("metrics")
            if isinstance(metrics, dict):
                score = metrics.get("llm_performance_score")
                if score is not None:
                    # Database stores scores on 0-100 scale, convert to 0-1
                    history_scores.append(float(score) / 100.0)

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

        return {
            "score": round(current_score, 3),
            "total_calls": total_calls,
            "failed_calls": failed,
            "fail_rate": round(fail_rate, 3),
            "historical_average": round(avg_score, 3),
            "issues": issues,
        }
    except Exception as e:
        logger.warning("[LLM-AUDIT] Failed to audit traces: %s", e)
        return {"score": 0.5, "issues": []}
