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

        _update_ar_state(report_id, phase="data_quality")
        data_quality = _audit_data_quality(tickers)

        _update_ar_state(report_id, phase="decision_quality")
        decision_quality = _audit_decisions(cycle_id, cycle_summary)

        _update_ar_state(report_id, phase="llm_traces")
        llm_analysis = _audit_llm_traces(cycle_id)

        _update_ar_state(report_id, phase="performance")
        perf_metrics = _audit_performance(cycle_id, cycle_summary)

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

        # Evolutionary Debate Council
        _EVO_TIMEOUT = 60
        try:
            from app.pipeline.analysis.evolution_router import router
            await asyncio.wait_for(router.run_router(cycle_id), timeout=_EVO_TIMEOUT)
        except Exception as e:
            logger.error("[AUTORESEARCH] Failed to trigger Evolution Router: %s", e)

        # Directives generation
        _update_ar_state(report_id, phase="directives")
        try:
            _generate_directives(reflection, cycle_id, triage_audit)
            _expire_old_directives()
        except Exception as dir_err:
            logger.warning("[AUTORESEARCH] Directive generation failed: %s", dir_err)

        # Benchmark Agent (Constitution review)
        _BENCH_TIMEOUT = 60
        try:
            from app.pipeline.analysis.benchmark_agent import run_benchmark_agent
            await asyncio.wait_for(run_benchmark_agent(cycle_id), timeout=_BENCH_TIMEOUT)
        except Exception as bench_err:
            logger.error("[AUTORESEARCH] Failed to trigger Benchmark Agent: %s", bench_err)

        # Record subsystem benchmarks
        try:
            from app.pipeline.subsystem_benchmarks import record_all
            record_all(cycle_id)
        except Exception as sb_err:
            logger.warning("[AUTORESEARCH] Subsystem benchmark recording failed: %s", sb_err)

        # Probation Rollbacks
        try:
            from app.cognition.evolution.rollback_monitor import check_probation_fixes
            rollback_summary = check_probation_fixes(cycle_id)
            if rollback_summary.get("rolled_back", 0) > 0:
                logger.warning("[AUTORESEARCH] Rolled back %d degrading fixes!", rollback_summary["rolled_back"])
        except Exception as rb_err:
            logger.warning("[AUTORESEARCH] Rollback monitor failed: %s", rb_err)

        # Meta-Agent Judge (prompt lifecycle management)
        _META_JUDGE_TIMEOUT = 120
        try:
            from app.agents.meta_agent_judge import run_meta_agent_judge
            _update_ar_state(report_id, phase="meta_judge")
            meta_result = await asyncio.wait_for(
                run_meta_agent_judge(cycle_id), timeout=_META_JUDGE_TIMEOUT
            )
            if meta_result.get("status") != "disabled":
                logger.info(
                    "[AUTORESEARCH] Meta-Agent Judge: benched=%d, promoted=%d, generated=%d",
                    len(meta_result.get("benched", [])),
                    len(meta_result.get("promoted", [])),
                    len(meta_result.get("generated", [])),
                )
        except Exception as mj_err:
            logger.warning("[AUTORESEARCH] Meta-Agent Judge failed: %s", mj_err)

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

async def run_partial_autoresearch(cycle_id: str, tickers: list[str]) -> dict:
    logger.info(f"[AUTORESEARCH] Running partial mid-cycle autoresearch for {len(tickers)} tickers.")
    data_quality = _audit_data_quality(tickers)
    return {"status": "partial_done", "data_quality": data_quality}

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

            if occurrence_row and occurrence_row[0] >= 5:
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
