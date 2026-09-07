import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator


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
        f"schedule_recommendation (optional string or null).\n"
        "Keep current cycle health separate from historical prediction quality. "
        "An old outcome cohort does not establish a current harness failure. "
        "Do not infer missing evidence from prompt size or loop count; use the "
        "recorded delivery manifest. An absent manifest means unverified.\n\n"
        f"Data quality: {data_q.get('avg_score', 0):.0%}, gaps: {len(data_q.get('gaps', []))}\n"
        f"Decisions: {dec_q.get('buy', 0)} BUY, {dec_q.get('sell', 0)} SELL, {dec_q.get('hold', 0)} HOLD\n"
    )

    outcome_stats = dec_q.get("outcome_stats", {})
    if outcome_stats.get("scoring_method") == "outcome_based":
        # The cohort is resolved DECISIONS, most of which are holds. Print the
        # whole breakdown: an earlier version printed only total + W/L/F, so
        # the reflection LLM was handed "100 resolved trades" next to a 9-trade
        # breakdown (the other 91 rows were HOLD_* outcomes it never saw) and
        # faithfully reported the system as internally inconsistent.
        total = outcome_stats.get('total_resolved', 0)
        traded = (outcome_stats.get('wins', 0) + outcome_stats.get('losses', 0)
                  + outcome_stats.get('flats', 0))
        holds = (outcome_stats.get('holds_right', 0)
                 + outcome_stats.get('holds_miss', 0))
        hold_acc = outcome_stats.get('hold_accuracy')
        cap_note = " (window capped at 100 — true 30d count may be higher)" if total >= 100 else ""
        prompt += (
            f"\n=== HISTORICAL PREDICTION ACCURACY (resolved in last 30 days) ===\n"
            f"Median decision age: {outcome_stats.get('median_decision_age_days', 'unmeasured')} days. "
            "This cohort may predate the current harness; it is not this cycle’s outcome.\n"
            f"Resolved decisions: {total}{cap_note} = "
            f"{traded} executed trades + {holds} hold calls\n"
            f"Executed trades: {outcome_stats.get('wins', 0)}W / "
            f"{outcome_stats.get('losses', 0)}L / {outcome_stats.get('flats', 0)}F, "
            f"win rate {outcome_stats.get('win_rate', 0):.0%} "
            f"(basis: {outcome_stats.get('win_rate_basis', 'ex_flat_ex_hold')})\n"
            f"Hold calls: {outcome_stats.get('holds_correct', 0)} correct + "
            f"{outcome_stats.get('holds_avoided_decline', 0)} avoided-decline / "
            f"{outcome_stats.get('holds_miss', 0)} missed"
            + (f", hold accuracy {hold_acc:.0%}\n" if hold_acc is not None else "\n")
            + f"Avg win: +{outcome_stats.get('avg_win_pnl', 0):.1f}% | Avg loss: -{outcome_stats.get('avg_loss_pnl', 0):.1f}%\n"
            f"Conviction calibration: {outcome_stats.get('calibration_score', 0):.0%}\n"
            f"Risk score: {outcome_stats.get('risk_score', 0):.0%}\n"
            f"=== END PREDICTION ACCURACY ===\n\n"
        )
    else:
        prompt += f"Prediction accuracy: INSUFFICIENT DATA ({outcome_stats.get('note', 'cold start')})\n"

    # Filter out self-referential error messages from LLM parsing/canary failures
    clean_exec_errs = [
        err for err in exec_errs
        if not (
            isinstance(err, dict)
            and any(
                k in str(err.get("error_message", "")).lower()
                for k in ("failed to parse json", "think-leak", "empty response text", "all auditors failed")
            )
        )
    ]

    # total_calls counts this cycle's agent runs (v3_agent_telemetry). When the
    # audit could not measure (no telemetry rows), say "unmeasured" — a
    # fabricated 0 here once led the reflection LLM to conclude the decision
    # engine never ran on a perfectly healthy cycle.
    _calls = llm_a.get('total_calls')
    if llm_a.get('availability') is None and not _calls:
        llm_line = "Agent LLM runs this cycle: unmeasured (no telemetry rows)"
    else:
        llm_line = (f"Agent LLM runs this cycle: {_calls or 0}, "
                    f"failed runs: {llm_a.get('failed_calls', 0)}")
    prompt += (
        f"{llm_line}\n"
        f"Duration: {perf.get('total_ms', 0) / 1000:.1f}s\n"
        f"Recovery failures: {recovery.get('total_failures', 0)}\n"
        f"{sched_line}\n"
        f"Data gaps: {safe_dumps(data_q.get('gaps', [])[:3])}\n"
        f"Issues: {safe_dumps(dec_q.get('issues', [])[:3])}\n"
        f"Schedule issues: {safe_dumps(sched.get('issues', [])[:3])}\n"
        f"System Execution Errors: {safe_dumps(clean_exec_errs)}"
    )

    prompt += "\nContext delivery evidence: " + safe_dumps(audit_bundle.get("context_delivery", {"availability": "unverified"}))

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
        from app.services.learning.records import recall_incidents
        gaps = audit_bundle.get("data_quality", {}).get("gaps", [])
        issues = audit_bundle.get("decision_quality", {}).get("issues", [])
        query = " ".join(str(x) for x in gaps[:3] + issues[:3])[:1500]
        return "\n".join(
            f"- [unresolved recommendation; id={row['id'][:12]}] {row['text']}"
            for row in recall_incidents(query)
        )

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

def _store_lessons(reflection: dict, cycle_id: str) -> dict:
    from app.services.learning.records import write
    result = {"stored": 0, "failed": 0}
    for rec in (reflection.get("recommendations") or [])[:3]:
        if not isinstance(rec, str) or len(rec.strip()) < 10:
            continue
        try:
            write(rec, cycle_id=cycle_id, producer="autoresearch_reflection",
                  kind="incident", source_refs=[f"autoresearch_reports:{cycle_id}"],
                  evidence=str(reflection.get("summary") or ""))
            result["stored"] += 1
        except Exception as exc:
            result["failed"] += 1
            logger.error("[AUTORESEARCH] Lesson write failed for %s: %s", cycle_id, exc)
    return result
