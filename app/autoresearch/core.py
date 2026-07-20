import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


from app.autoresearch.auditors.data_audit import _audit_data_quality
from app.autoresearch.auditors.decision_audit import _audit_decisions
from app.autoresearch.auditors.llm_audit import _audit_llm_traces
from app.autoresearch.auditors.performance_audit import _audit_performance, _audit_recovery, _audit_execution_errors
from app.autoresearch.auditors.triage_audit import _audit_triage
from app.autoresearch.auditors.schedule_audit import _audit_schedule_health
from app.autoresearch.reflection import _reflect, _store_lessons
from app.autoresearch.directives import _generate_directives, _expire_old_directives
from app.autoresearch.outcome_tracker import record_cycle_decisions, resolve_pending_outcomes
from app.autoresearch.janitor import run_janitor

def _update_ar_state(report_id: str, **kwargs):
    updates = []
    params = []
    for k, v in kwargs.items():
        if k == "running":
            updates.append("status = %s")
            params.append("running" if v else "done")
        else:
            updates.append(f"{k} = %s")
            params.append(v)
    if not updates:
        return
    params.append(report_id)
    try:
        with get_db() as db:
            db.execute(
                f"UPDATE autoresearch_reports SET {', '.join(updates)} WHERE id = %s",
                params,
            )
    except Exception as e:
        logger.debug("Failed to update ar state: %s", e)

def get_autoresearch_status() -> dict:
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT cycle_id, status, phase, error, created_at "
                "FROM autoresearch_reports ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row:
                return {
                    "running": row[1] == "running",
                    "cycle_id": row[0],
                    "phase": row[2] or "",
                    "error": row[3],
                    "started_at": row[4],
                }
    except Exception:
        pass
    return {
        "running": False,
        "cycle_id": None,
        "phase": None,
        "error": None,
        "started_at": None,
    }

def _collect_learning_signals(cycle_id: str) -> dict:
    """Per-ticker learning_signal from the Decision Synthesizer's trade_decision.

    The synthesizer reports what past-cycle memory actually changed
    (lessons_applied / outcome_correlation / similar_past_cycles); it lives at
    shared_desk.desk_data->'trade_decision'->'learning_signal'.
    """
    signals: dict = {}
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT ticker, desk_data->'trade_decision'->'learning_signal' "
                "FROM shared_desk WHERE cycle_id = %s",
                (cycle_id,),
            ).fetchall()
            for ticker, sig in rows:
                if sig:
                    signals[ticker] = sig
    except Exception as e:
        logger.debug("[AUTORESEARCH] learning_signal collection failed: %s", e)
    return signals


async def run_autoresearch(cycle_id: str, cycle_summary: dict) -> dict:
    """Main entry point: run full autoresearch after a cycle."""
    report_id = f"ar-{uuid.uuid4().hex[:12]}"
    tickers = (
        cycle_summary.get("tickers_final")
        or cycle_summary.get("tickers_requested")
        or []
    )

    try:
        from app.utils.trace import set_trace_id
        set_trace_id(report_id)

        # Clean up stale reports stuck in 'running' from previous crashed cycles
        with get_db() as db:
            try:
                db.execute(
                    "UPDATE autoresearch_reports SET status = 'stale' "
                    "WHERE status = 'running' AND created_at < NOW() - INTERVAL '30 minutes'"
                )
                cleaned = db.execute(
                    "SELECT COUNT(*) FROM autoresearch_reports WHERE status = 'stale'"
                ).fetchone()
                if cleaned and cleaned[0] > 0:
                    logger.info(
                        "[AUTORESEARCH] Cleaned up stale 'running' reports (total stale: %d)",
                        cleaned[0],
                    )
            except Exception as cleanup_err:
                logger.debug("[AUTORESEARCH] Stale cleanup skipped: %s", cleanup_err)

            db.execute(
                "INSERT INTO autoresearch_reports (id, cycle_id, status, phase) VALUES (%s, %s, 'running', 'starting')",
                (report_id, cycle_id),
            )

        # Resolve pending decision outcomes before scoring
        _update_ar_state(report_id, phase="outcome_resolution")
        try:
            outcome_result = resolve_pending_outcomes()
            try:
                from app.v3.challenger import resolve_challenger_outcomes
                resolve_challenger_outcomes()
            except Exception as ch_err:
                logger.debug("Challenger resolution skipped: %s", ch_err)
            if outcome_result.get("resolved", 0) > 0:
                logger.info("[AUTORESEARCH] Resolved %d pending outcomes", outcome_result["resolved"])
        except Exception as oe:
            logger.warning("[AUTORESEARCH] Outcome resolution failed: %s", oe)

        # LLM-as-a-Judge: grade this cycle's decisions (llm_audit_logs rows are
        # written per ticker by the V3 orchestrator's _persist_trade_verdict).
        # Time-boxed so a judge-LLM slowdown can't stall the whole job; the
        # global fallback inside evaluate_pending_decisions also slowly chews
        # through historical backlog when a cycle has nothing to judge.
        _update_ar_state(report_id, phase="judge_eval")
        try:
            from app.cognition.evaluation.strategy_auditor import evaluate_pending_decisions
            with get_db() as db:
                judged = await evaluate_pending_decisions(
                    db, cycle_id=cycle_id, limit=10, timeout_sec=240,
                )
            if judged:
                logger.info("[AUTORESEARCH] Judge evaluated %d decisions", judged)
        except Exception as je:
            logger.warning("[AUTORESEARCH] Judge evaluation failed (non-fatal): %s", je)

        # Bot-level strategy audit — the consumer of decision_evaluations.
        # evaluate_strategy/compute_agent_metrics previously had ZERO callers,
        # so the judge's grades landed nowhere. Persists strategy_evaluations.
        _update_ar_state(report_id, phase="strategy_eval")
        try:
            from app.cognition.evaluation.strategy_auditor import evaluate_strategy
            strat = await asyncio.wait_for(
                evaluate_strategy(cycle_id=cycle_id), timeout=120,
            )
            if strat:
                logger.info(
                    "[AUTORESEARCH] Strategy audit: %d decisions evaluated",
                    (strat.get("agent_metrics") or {}).get("total_decisions_evaluated", 0)
                    if isinstance(strat.get("agent_metrics"), dict) else 0,
                )
        except Exception as se:
            logger.warning("[AUTORESEARCH] Strategy audit failed (non-fatal): %s", se)

        _update_ar_state(report_id, phase="data_quality")
        data_quality = _audit_data_quality(tickers)

        _update_ar_state(report_id, phase="decision_quality")
        decision_quality = _audit_decisions(cycle_id, cycle_summary)

        _update_ar_state(report_id, phase="llm_traces")
        llm_analysis = _audit_llm_traces(cycle_id)

        _update_ar_state(report_id, phase="performance")
        perf_metrics = _audit_performance(cycle_id, cycle_summary)
        # Persist score provenance next to the number: version (formula
        # changes must be attributable), cohort n/age (rolling-term drift is
        # cohort drift, not system change), and the per-cycle judge subscore
        # (the only component that can move on a single cycle).
        def _jsonsafe(v):
            # DB-sourced numerics can be Decimal, which strict json.dumps
            # rejects — this block is persisted verbatim into the report row.
            return float(v) if isinstance(v, (int, float)) or hasattr(v, "__float__") else v

        perf_metrics["decision_cohort"] = {
            "score_version": decision_quality.get("score_version"),
            "per_cycle_judge_score": _jsonsafe(decision_quality.get("per_cycle_judge_score")),
            **{
                k: _jsonsafe(decision_quality.get("outcome_stats", {}).get(k))
                for k in ("cohort_n", "cohort_window_days", "median_decision_age_days",
                          "hold_accuracy", "win_rate", "calibration_ece")
            },
        }

        _update_ar_state(report_id, phase="recovery")
        recovery = _audit_recovery()
        exec_errors = _audit_execution_errors(cycle_id)

        _update_ar_state(report_id, phase="reflection")
        audit_bundle = {
            "cycle_id": cycle_id,
            "tickers": tickers,
            "data_quality": data_quality,
            "decision_quality": decision_quality,
            "llm_analysis": llm_analysis,
            "performance": perf_metrics,
            "recovery": recovery,
            "execution_errors": exec_errors,
            # The Decision Synthesizer's per-ticker learning_signal (what past
            # memory actually changed this cycle) — produced and persisted every
            # cycle, but reflection never saw it until now.
            "learning_signals": _collect_learning_signals(cycle_id),
        }

        # Triage audit (evaluate triage distribution + attention health)
        _update_ar_state(report_id, phase="triage_audit")
        triage_audit = _audit_triage(cycle_id, cycle_summary, tickers)
        audit_bundle["triage_audit"] = triage_audit

        # Schedule health audit
        _update_ar_state(report_id, phase="schedule_audit")
        schedule_health = _audit_schedule_health()
        audit_bundle["schedule_health"] = schedule_health

        _update_ar_state(report_id, phase="reflection")
        reflection = await _reflect(audit_bundle)

        data_score = data_quality.get("avg_score", 0) * 100
        decision_score = decision_quality.get("score", 0) * 100
        llm_score = llm_analysis.get("score", 0) * 100
        overall = (data_score + decision_score + llm_score) / 3

        # Degenerate score detection
        degenerate_subs = []
        if data_score == 0.0:
            degenerate_subs.append("data")
        if decision_score == 0.0:
            degenerate_subs.append("decision")
        if llm_score == 0.0:
            degenerate_subs.append("llm")
        if degenerate_subs:
            logger.warning(
                "[AUTORESEARCH] DEGENERATE SCORES: %s at 0.0 — Flagging as anomaly.",
                ", ".join(degenerate_subs)
            )
            reflection["anomaly"] = True
            reflection["anomaly_detail"] = f"Degenerate sub-scores at 0.0: {', '.join(degenerate_subs)}"

        with get_db() as db:
            db.execute(
                """UPDATE autoresearch_reports SET
                    data_quality_score= %s, decision_quality_score= %s, llm_performance_score= %s,
                    overall_score= %s, data_gaps= %s, decision_issues= %s, llm_issues= %s,
                    performance_metrics= %s, reflection= %s, recovery_stats= %s, status='done'
                WHERE id=%s""",
                [
                    round(data_score, 1),
                    round(decision_score, 1),
                    round(llm_score, 1),
                    round(overall, 1),
                    json.dumps(data_quality.get("gaps", [])),
                    json.dumps(decision_quality.get("issues", [])),
                    json.dumps(llm_analysis.get("issues", [])),
                    json.dumps(perf_metrics),
                    json.dumps(reflection),
                    json.dumps(recovery),
                    report_id,
                ],
            )

        try:
            _store_lessons(reflection, cycle_id)

            if reflection.get("system_health") == "critical":
                from app.services.session_profile import profile_memory
                summary = reflection.get("summary", "Critical health detected by autoresearch.")
                profile_memory.add_agent_note(f"⚠️ AUTORESEARCH CRITICAL WARNING (Cycle {cycle_id[:8]}): {summary}")
        except Exception as ls_err:
            logger.warning("[AUTORESEARCH] Lesson store write failed: %s", ls_err)

        # SkillOpt: propose + validate per-agent skill-doc edits from this
        # cycle's reflection. Time-boxed internally and never fatal — a skill
        # mutation failure must not block the rest of the pipeline.
        _update_ar_state(report_id, phase="skill_mutation")
        try:
            from app.autoresearch.skill_optimizer import propose_and_validate_skill_edits
            skill_summary = await propose_and_validate_skill_edits(reflection, cycle_id, tickers)
            logger.info("[AUTORESEARCH] SkillOpt: %s", skill_summary)
        except Exception as sk_err:
            logger.warning("[AUTORESEARCH] Skill mutation skipped (non-fatal): %s", sk_err)

        # Auto-resolve detected data gaps
        _update_ar_state(report_id, phase="gap_resolution")
        try:
            gap_result = await _resolve_data_gaps(data_quality.get("gaps", []), cycle_id)
            logger.info(
                "[AUTORESEARCH] Data gap resolution: resolved=%d, failed=%d, banned=%d",
                gap_result.get("resolved", 0), gap_result.get("failed", 0), gap_result.get("banned", 0)
            )
        except Exception as gap_err:
            logger.warning("[AUTORESEARCH] Data gap resolution failed: %s", gap_err)

        # (Evolutionary Debate Council removed — app.pipeline.analysis.evolution_router
        # was deleted in the V3 purge; the import failed silently every cycle.)

        # Directives generation
        _update_ar_state(report_id, phase="directives")
        try:
            _generate_directives(reflection, cycle_id, triage_audit)
            _expire_old_directives()
        except Exception as dir_err:
            logger.warning("[AUTORESEARCH] Directive generation failed: %s", dir_err)

        # (Benchmark Agent and subsystem-benchmark recording removed — their
        # app.pipeline.* modules were deleted in the V3 purge; the imports
        # failed silently every cycle.)

        # Probation Rollbacks
        try:
            from app.cognition.evolution.rollback_monitor import check_probation_fixes
            rollback_summary = check_probation_fixes(cycle_id)
            if rollback_summary.get("rolled_back", 0) > 0:
                logger.warning("[AUTORESEARCH] Rolled back %d degrading fixes!", rollback_summary["rolled_back"])
        except Exception as rb_err:
            logger.warning("[AUTORESEARCH] Rollback monitor failed: %s", rb_err)

        # (Meta-Agent Judge removed: app.agents.meta_agent_judge never existed
        # in the V3 tree — the import failed and logged a warning every cycle.)

        # Record this cycle's decisions for future outcome tracking
        _update_ar_state(report_id, phase="outcome_recording")
        try:
            recorded = record_cycle_decisions(cycle_id, cycle_summary)
            if recorded > 0:
                logger.info("[AUTORESEARCH] Recorded %d decision outcomes for tracking", recorded)
        except Exception as rec_err:
            logger.warning("[AUTORESEARCH] Decision recording failed: %s", rec_err)

        # Run janitor to clean up old data
        _update_ar_state(report_id, phase="cleanup")
        try:
            janitor_result = run_janitor()
        except Exception as jan_err:
            logger.warning("[AUTORESEARCH] Janitor failed: %s", jan_err)

        _update_ar_state(report_id, phase="done")
        return {"id": report_id, "overall_score": round(overall, 1), "status": "done"}

    except Exception as e:
        logger.error("[AUTORESEARCH] Failed: %s", e, exc_info=True)
        _update_ar_state(report_id, error=str(e), phase="error")
        try:
            with get_db() as db:
                db.execute("UPDATE autoresearch_reports SET status='error' WHERE id=%s", (report_id,))
        except Exception:
            pass
        return {"error": str(e)}
    finally:
        try:
            with get_db() as db:
                status = db.execute("SELECT status FROM autoresearch_reports WHERE id=%s", [report_id]).fetchone()
                if status and status[0] == 'running':
                    _update_ar_state(report_id, running=False)
        except:
            pass


async def _resolve_data_gaps(gaps: list[dict], cycle_id: str) -> dict:
    if not gaps: return {"resolved": 0, "failed": 0, "banned": 0}
    resolved = 0
    failed = 0
    banned = 0

    COLLECTOR_MAP = {
        "news": ("app.collectors.news_collector", "collect_for_ticker"),
        "price_history": ("app.collectors.yfinance_collector", "collect_price_history"),
        "technicals": ("app.processors.technical_processor", "compute_technicals"),
        "fundamentals": ("app.collectors.yfinance_collector", "collect_fundamentals"),
    }

    for gap in gaps[:5]:
        ticker = gap.get("ticker", "")
        missing = gap.get("missing_sources", [])
        if not ticker or not missing: continue

        try:
            with get_db() as db:
                occurrence_row = db.execute(
                    "SELECT COUNT(*) FROM autoresearch_reports WHERE status = 'done' AND data_gaps LIKE %s",
                    [f'%"{ticker}"%']
                ).fetchone()

            if occurrence_row and occurrence_row[0] >= 3:
                from app.trading.watchlist import ban_ticker
                ban_ticker(ticker, f"AutoResearch: persistent data gap across {occurrence_row[0]} cycles")
                banned += 1
                continue
        except Exception as ban_err:
            logger.debug("Ban check failed for %s: %s", ticker, ban_err)

        import importlib
        for source in missing:
            collector_info = COLLECTOR_MAP.get(source)
            if not collector_info: continue
            module_path, func_name = collector_info
            try:
                mod = importlib.import_module(module_path)
                collect_fn = getattr(mod, func_name)

                if asyncio.iscoroutinefunction(collect_fn):
                    await asyncio.wait_for(collect_fn(ticker), timeout=30.0)
                else:
                    collect_fn(ticker)
                resolved += 1
            except Exception as coll_err:
                failed += 1
                logger.warning("Gap resolution failed: %s/%s — %s", ticker, source, coll_err)

    return {"resolved": resolved, "failed": failed, "banned": banned}
