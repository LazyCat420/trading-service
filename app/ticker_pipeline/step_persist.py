"""
Step: Persist — Memory write-back, DB logging, post-cycle hooks.

Writes the final result to:
  - Episodic memory (for future cycles to learn from)
  - analysis_results table (for frontend display)
  - Post-cycle hooks (watchlist updates, reports)
  - Attention tracker (analysis frequency tracking)

Extracted from runner.py Steps 7-11.
"""

import asyncio
import logging
import time
from typing import Any

from app.cognition.orchestration.models import CognitionRunResult
from app.core.result_builder import build_v1_compatible_result
from app.core.emit_helpers import emit_decision
from app.ticker_pipeline.context import TickerContext
from app.log_manager import log_manager

logger = logging.getLogger(__name__)


async def run_persist_step(ctx: TickerContext) -> dict[str, Any]:
    """Persist results to DB, memory, hooks, and return the final result dict."""
    from app.pipeline.orchestration.cycle_control import cycle_control

    # ── Step 7: Episodic memory write-back ──
    try:
        from app.cognition.memory.writer import write_episode

        await cycle_control.wait_if_paused()
        run_result = CognitionRunResult(
            entity_id=ctx.ticker,
            cycle_id=ctx.cycle_id,
            final_action=ctx.final_action,
            final_confidence=ctx.final_confidence,
            summary=ctx.final_rationale[:500],
            rationale=ctx.final_rationale,
            tags=[ctx.ticker.lower(), "v2_stage"],
            evidence_packet=ctx.packet,
            thesis=ctx.thesis,
            sufficiency=ctx.sufficiency,
            memory_context=ctx.memory_context,
            total_tokens=ctx.total_tokens,
            total_ms=ctx.elapsed_ms(),
            stages_completed=ctx.stages,
            retrieval_retries=ctx.retrieval_retries,
        )
        if ctx.db_semaphore:
            async with ctx.db_semaphore:
                episode_id = write_episode(run_result)
        else:
            episode_id = write_episode(run_result)
        ctx.add_stage("memory_write")
        logger.info("[V2] Wrote episode %s for %s", episode_id[:8] if episode_id else "?", ctx.ticker)
    except Exception as e:
        logger.warning("[V2] Memory write failed for %s (non-fatal): %s", ctx.ticker, e)

    # ── Step 8: V2 cycle log ──
    try:
        log_manager.log_v2_cycle(
            cycle_id=ctx.cycle_id, step_name="v2_pipeline_complete",
            payload={
                "ticker": ctx.ticker, "action": ctx.final_action,
                "confidence": ctx.final_confidence,
                "stages": ctx.stages, "retrieval_retries": ctx.retrieval_retries,
                "claims_count": len(ctx.packet.claims),
                "missing_fields": ctx.packet.missing_fields,
                "sufficiency_status": ctx.sufficiency.status,
            },
        )
        ctx.add_stage("v2_log")
    except Exception as e:
        logger.warning("[V2] Log write failed: %s", e)

    # ── Build V1-compatible result ──
    elapsed = ctx.elapsed_s()

    # Pipeline complete summary log
    _timing_str = ", ".join(f"{k}:{v}ms" for k, v in ctx.stage_timings.items())
    logger.info(
        "[V2] PIPELINE_COMPLETE ticker=%s elapsed=%dms tokens=%d action=%s confidence=%d stages=[%s]",
        ctx.ticker, int(elapsed * 1000), ctx.total_tokens,
        ctx.final_action, ctx.final_confidence, _timing_str,
    )

    # Emit final decision
    emit_decision(
        ctx.emit or (lambda *a, **kw: None),
        ctx.ticker, ctx.final_action, ctx.final_confidence,
        elapsed, ctx.total_tokens,
        rationale=ctx.final_rationale,
    )

    formatted_agent_results = {}
    if ctx.agent_insights:
        for k, v in ctx.agent_insights.items():
            formatted_agent_results[k] = {
                "response": v if isinstance(v, str) else str(v),
                "tokens": 0,
            }

    result = build_v1_compatible_result(
        ticker=ctx.ticker,
        action=ctx.final_action,
        confidence=ctx.final_confidence,
        rationale=ctx.final_rationale,
        cycle_id=ctx.cycle_id,
        total_tokens=ctx.total_tokens,
        elapsed=elapsed,
        stages=ctx.stages,
        config_used="v2_cognition",
        thesis=ctx.thesis,
        sufficiency=ctx.sufficiency,
        memory_context=ctx.memory_context,
        debate_result=ctx.debate_result,
        agent_results=formatted_agent_results,
    )

    # ── Attach report data ──
    try:
        _structured_facts = [
            {
                "field_name": getattr(fact, "field_name", str(fact)),
                "value": getattr(fact, "value", None),
                "source": getattr(fact, "source", "unknown"),
            }
            for fact in ctx.packet.structured_facts[:100]
        ]
    except Exception:
        _structured_facts = []

    result["_report_data"] = {
        "agent_insights": ctx.agent_insights or {},
        "debate_result": ctx.debate_result,
        "thesis": ctx.thesis,
        "sufficiency": ctx.sufficiency,
        "stages": list(ctx.stages),
        "stage_timings": dict(ctx.stage_timings),
        "hallucination_result": ctx.hallucination_result,
        "memory_brief": ctx.memory_context.get("memory_brief", ""),
        "present_sources": [],
        "missing_sources": ctx.packet.missing_fields if ctx.packet else [],
        "freshness_summary": ctx.packet.freshness_summary if ctx.packet else None,
        "structured_facts": _structured_facts,
        "failure_diagnosis": ctx.failure_diagnosis,
    }

    # ── Step 9: Log decision to analysis_results ──
    try:
        from app.core.db_writer import log_decision

        if ctx.db_semaphore:
            async with ctx.db_semaphore:
                log_decision(result, ctx.cycle_id, ctx.bot_id)
        else:
            log_decision(result, ctx.cycle_id, ctx.bot_id)
        ctx.add_stage("db_log")
    except Exception as e:
        logger.warning("[V2] log_decision failed for %s (non-fatal): %s", ctx.ticker, e)

    # ── Step 10: Post-cycle hooks ──
    try:
        from app.pipeline.orchestration.post_cycle_hooks import run_post_cycle_hooks

        await run_post_cycle_hooks(
            ticker=ctx.ticker, result=result, escalated=False,
            cycle_id=ctx.cycle_id,
            final_action=ctx.final_action,
            final_confidence=ctx.final_confidence,
        )
        ctx.add_stage("post_cycle_hooks")
    except Exception as e:
        logger.warning("[V2] Post-cycle hooks failed for %s (non-fatal): %s", ctx.ticker, e)

    # ── Step 11: Attention tracker ──
    try:
        from app.pipeline.attention_tracker import record_analysis as _record_attn

        _record_attn(
            ctx.ticker,
            action=ctx.final_action,
            confidence=ctx.final_confidence,
            was_deep=True,
        )
        ctx.add_stage("attention_record")
    except Exception as e:
        logger.warning("[V2] Attention tracker failed for %s (non-fatal): %s", ctx.ticker, e)

    return result
