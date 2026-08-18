import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


async def _reflect(audit_bundle: dict) -> dict:
    data_q = audit_bundle.get("data_quality", {})
    dec_q = audit_bundle.get("decision_quality", {})
    llm_a = audit_bundle.get("llm_analysis", {})
    perf = audit_bundle.get("performance", {})
    recovery = audit_bundle.get("recovery", {})
    sched = audit_bundle.get("schedule_health", {})
    exec_errs = audit_bundle.get("execution_errors", [])

    class DateTimeEncoder(json.JSONEncoder):
        def default(self, obj):
            if hasattr(obj, 'isoformat'): return obj.isoformat()
            return super().default(obj)

    def safe_dumps(obj):
        return json.dumps(obj, cls=DateTimeEncoder)

    sched_line = (
        f"Schedules: {sched.get('active_count', 0)} active, "
        f"avg interval {sched.get('avg_interval_hours', 'N/A')}h, "
        f"issues: {len(sched.get('issues', []))}"
    )

    prompt = (
        f"Review this trading cycle audit. Provide JSON with: summary, recommendations (list of 3), "
        f"urgent_data_gaps (ticker list), system_health (healthy/degraded/critical), "
        f"schedule_recommendation (optional string or null).\n\n"
        f"Data quality: {data_q.get('avg_score', 0):.0%}, gaps: {len(data_q.get('gaps', []))}\n"
        f"Decisions: {dec_q.get('buy', 0)} BUY, {dec_q.get('sell', 0)} SELL, {dec_q.get('hold', 0)} HOLD\n"
    )

    outcome_stats = dec_q.get("outcome_stats", {})
    if outcome_stats.get("scoring_method") == "outcome_based":
        prompt += (
            f"\n=== PREDICTION ACCURACY (last 30 days) ===\n"
            f"Resolved trades: {outcome_stats.get('total_resolved', 0)}\n"
            f"Win rate: {outcome_stats.get('win_rate', 0):.0%} "
            f"({outcome_stats.get('wins', 0)}W / {outcome_stats.get('losses', 0)}L / {outcome_stats.get('flats', 0)}F)\n"
            f"Avg win: +{outcome_stats.get('avg_win_pnl', 0):.1f}% | Avg loss: -{outcome_stats.get('avg_loss_pnl', 0):.1f}%\n"
            f"Conviction calibration: {outcome_stats.get('calibration_score', 0):.0%}\n"
            f"Risk score: {outcome_stats.get('risk_score', 0):.0%}\n"
            f"=== END PREDICTION ACCURACY ===\n\n"
        )
    else:
        prompt += f"Prediction accuracy: INSUFFICIENT DATA ({outcome_stats.get('note', 'cold start')})\n"

    prompt += (
        f"LLM calls: {llm_a.get('total_calls', 0)}, failures: {llm_a.get('failed_calls', 0)}\n"
        f"Duration: {perf.get('total_ms', 0) / 1000:.1f}s\n"
        f"Recovery failures: {recovery.get('total_failures', 0)}\n"
        f"{sched_line}\n"
        f"Data gaps: {safe_dumps(data_q.get('gaps', [])[:3])}\n"
        f"Issues: {safe_dumps(dec_q.get('issues', [])[:3])}\n"
        f"Schedule issues: {safe_dumps(sched.get('issues', [])[:3])}\n"
        f"System Execution Errors: {safe_dumps(exec_errs)}"
    )

    learning_signals = audit_bundle.get("learning_signals") or {}
    if learning_signals:
        prompt += (
            f"\n\n=== SYNTHESIZER LEARNING SIGNALS (what past-cycle memory changed) ===\n"
            f"{safe_dumps(learning_signals)[:3000]}\n"
            f"Weigh these when writing recommendations: lessons the desk already "
            f"applied should not be re-recommended; lessons ignored deserve emphasis."
        )

    # Recall past stored lessons so reflection stops re-discovering the same
    # problems. This closes the lesson loop: _store_lessons has written these
    # every cycle since birth, but nothing ever read them back.
    past = _recall_past_lessons(audit_bundle)
    if past:
        prompt += (
            f"\n\n=== PAST LESSONS (already recorded — do NOT repeat verbatim) ===\n"
            f"{past}\n"
            f"Only re-issue one of these if it is still unresolved, and say so."
        )

    try:
        # vllm_client was replaced by the SDK-backed shim in c82526b; the old
        # import made this fail (silently) every cycle → canned fallback text.
        from app.services.prism_agent_caller import llm, Priority
        response, tokens, elapsed = await llm.chat(
            system="You are a trading system auditor. Output valid JSON only.",
            user=prompt,
            temperature=0.1,
            max_tokens=8192,
            agent_name="autoresearch_reflection",
            ticker="_system",
            priority=Priority.LOW
        )
        from app.utils.text_utils import parse_json_response
        parsed = parse_json_response(response)
        if parsed is None:
            logger.warning("[AUTORESEARCH] parse_json_response returned None, falling back to rule-based")
            return _rule_based_reflection(audit_bundle)
        parsed["tokens_used"] = tokens
        return parsed
    except Exception as e:
        logger.warning("[AUTORESEARCH] LLM reflection failed: %s", e)
        return _rule_based_reflection(audit_bundle)

def _recall_past_lessons(audit_bundle: dict) -> str:
    """Vector-recall previously stored lessons relevant to this cycle's issues.

    Non-fatal: '' on any failure or when nothing is stored yet.
    """
    try:
        from app.cognition.lesson_store import retrieve_lessons
        from app.constants import EVOLVE_COGNITION_K

        gaps = audit_bundle.get("data_quality", {}).get("gaps", [])
        issues = audit_bundle.get("decision_quality", {}).get("issues", [])
        query_parts = ["trading cycle recommendations"]
        query_parts += [g.get("ticker", "") for g in gaps[:3]]
        query_parts += [str(i.get("suggestion") or i.get("issue") or "") for i in issues[:3]]
        query = " ".join(p for p in query_parts if p)[:500]

        lessons = retrieve_lessons(query, k=EVOLVE_COGNITION_K)
        lines = []
        for l in lessons:
            text = (l.get("lesson_text") or l.get("preview") or "").strip()
            if text:
                lines.append(f"- {text[:160]}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("[AUTORESEARCH] past-lesson recall failed (non-fatal): %s", e)
        return ""


def _rule_based_reflection(audit_bundle: dict) -> dict:
    data_q = audit_bundle.get("data_quality", {})
    dec_q = audit_bundle.get("decision_quality", {})
    recs = [g.get("recommendation", "") for g in data_q.get("gaps", [])[:2]]
    recs += [i.get("suggestion", "") for i in dec_q.get("issues", []) if i.get("suggestion")]
    health = "healthy" if data_q.get("avg_score", 1) >= 0.5 else "degraded" if data_q.get("avg_score", 1) >= 0.3 else "critical"
    return {
        "summary": f"Cycle completed with {len(data_q.get('gaps', []))} data gaps. Health: {health}.",
        "recommendations": [r for r in recs if r][:3],
        "urgent_data_gaps": [g["ticker"] for g in data_q.get("gaps", []) if g.get("missing_sources")][:5],
        "system_health": health,
        "fallback": True,
    }

def _store_lessons(reflection: dict, cycle_id: str):
    recs = reflection.get("recommendations", [])
    if not recs: return
    try:
        from app.cognition.lesson_store import add_lesson
        from app.utils.poison_guard import is_poisoned
        for rec in recs[:3]:
            if not rec or len(rec) < 10: continue
            if is_poisoned(rec):
                logger.warning("[AUTORESEARCH] Poison guard blocked lesson: %.60s…", rec)
                continue
            add_lesson(
                text=rec[:120],
                metadata={
                    "session_id": f"autoresearch_{cycle_id[:8]}",
                    "round": 0, "score": 0, "status": "recommendation",
                    "source": "autoresearch", "timestamp": datetime.now(timezone.utc).isoformat()
                }
            )
    except Exception as e:
        logger.debug("Lesson store write failed: %s", e)

