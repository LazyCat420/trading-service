import json
import re
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator


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
from app.db import mongo_query, mongo_store

def _update_ar_state(report_id: str, **kwargs):
    updates: dict = {}
    for k, v in kwargs.items():
        if k == "running":
            updates["status"] = "running" if v else "done"
        else:
            updates[k] = v
    if not updates:
        return
    try:
        mongo_store.update_docs('autoresearch_reports', {'id': report_id},
                                {'$set': updates})
    except Exception as e:
        logger.debug("Failed to update ar state: %s", e)

def get_autoresearch_status() -> dict:
    try:
        row = mongo_query.find_row('autoresearch_reports', {}, ['cycle_id', 'status', 'phase', 'error', 'created_at'], sort=[('created_at', -1)])
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
        # desk_data->'trade_decision'->'learning_signal' is a JSONB path. Mongo
        # stores it as a nested field, but find_rows() cannot read one: a
        # dotted projection comes back NESTED, while _to_tuple() does a flat
        # doc.get("a.b.c") and would return None for every row. So take the
        # documents and walk the path here.
        for d in mongo_query.find_dicts('shared_desk', {'cycle_id': cycle_id}):
            sig = ((d.get('desk_data') or {}).get('trade_decision') or {}).get('learning_signal')
            if sig:
                signals[d.get('ticker')] = sig
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
        try:
            mongo_store.update_docs('autoresearch_reports', {'status': 'running', 'created_at': {'$lt': (datetime.now(timezone.utc) - timedelta(minutes=30))}}, {'$set': {'status': 'stale'}})
            cleaned = mongo_query.agg_row('autoresearch_reports', {'status': 'stale'}, [('count', None)])
            if cleaned and cleaned[0] > 0:
                logger.info(
                    "[AUTORESEARCH] Cleaned up stale 'running' reports (total stale: %d)",
                    cleaned[0],
                )
        except Exception as cleanup_err:
            logger.debug("[AUTORESEARCH] Stale cleanup skipped: %s", cleanup_err)

        # created_at was a Postgres column DEFAULT. Mongo has none, and readers
        # filter on it, so an omitted field means the report is invisible.
        mongo_store.insert_docs('autoresearch_reports', [{'id': report_id, 'cycle_id': cycle_id, 'status': 'running', 'phase': 'starting', 'created_at': datetime.now(timezone.utc)}])

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
            judged = await evaluate_pending_decisions(
                cycle_id=cycle_id, limit=10, timeout_sec=240,
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
                          "hold_accuracy", "win_rate", "calibration_ece",
                          "confidence_tau_win_rate", "confidence_tau_magnitude",
                          "confidence_predicts")
            },
        }

        # The full outcome breakdown, persisted whole. The client's Decision
        # Outcomes strip looks for `outcome_stats` and the subset above lacks
        # the W/L/F + hold counts, so its chips never rendered — the only
        # surviving framing was the reflection LLM's prose.
        perf_metrics["outcome_stats"] = {
            k: _jsonsafe(v)
            for k, v in decision_quality.get("outcome_stats", {}).items()
        }

        # What each stated confidence has actually earned. Reported every
        # cycle because the gap is the finding: 15.8 points of overstatement
        # and an inverted top bucket are invisible in a single ECE number.
        try:
            from app.autoresearch.confidence_calibration import calibration_map
            cmap = calibration_map()
            perf_metrics["confidence_calibration"] = {
                "inversions": cmap.get("inversions"),
                "mean_overstatement": cmap.get("mean_overstatement"),
                "buckets": cmap.get("buckets", []),
            }
        except Exception as e:  # noqa: BLE001 — reporting, never blocks a cycle
            logger.debug("[AR] calibration map unavailable: %s", e)

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

        mongo_store.update_docs('autoresearch_reports', {'id': report_id}, {'$set': {'data_quality_score': round(data_score, 1), 'decision_quality_score': round(decision_score, 1), 'llm_performance_score': round(llm_score, 1), 'overall_score': round(overall, 1), 'data_gaps': json.dumps(data_quality.get("gaps", [])), 'decision_issues': json.dumps(decision_quality.get("issues", [])), 'llm_issues': json.dumps(llm_analysis.get("issues", [])), 'performance_metrics': json.dumps(perf_metrics), 'reflection': json.dumps(reflection), 'recovery_stats': json.dumps(recovery), 'status': 'done'}})

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

        # (Probation rollbacks removed 2026-07-31 with the evolution deployer.
        # check_probation_fixes queried pending_evolution_fixes for rows with
        # status='deployed' and a probation window still open. That table was
        # frozen on 07-28 and nothing has deployed into it since, so the query
        # matched 0 rows and could never match more — measured, not assumed. It
        # ran once per cycle to do nothing.)

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
            mongo_store.update_docs('autoresearch_reports', {'id': report_id}, {'$set': {'status': 'error'}})
        except Exception:
            pass
        return {"error": str(e)}
    finally:
        try:
            status = mongo_query.find_row('autoresearch_reports', {'id': report_id}, ['status'])
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
            # data_gaps is stored as a json.dumps() STRING (see the report
            # update below), so `LIKE '%"TICKER"%'` is a substring match on
            # that text, not a structured containment test. re.escape keeps a
            # ticker with a regex metacharacter from matching the wrong rows.
            occurrences = mongo_query.count(
                'autoresearch_reports',
                {'status': 'done',
                 'data_gaps': {'$regex': f'"{re.escape(ticker)}"'}})

            if occurrences >= 3:
                from app.trading.watchlist import ban_ticker
                ban_ticker(ticker, f"AutoResearch: persistent data gap across {occurrences} cycles")
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
