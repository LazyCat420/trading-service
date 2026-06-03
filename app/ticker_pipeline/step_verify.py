"""
Step: Hallucination Check (Hard Safety Gate).

Verifies that the thesis doesn't contain claims unsupported by the evidence
packet. If hallucination rate exceeds threshold, downgrades to HOLD.

Extracted from runner.py Step 6.5.
"""

import logging

from app.ticker_pipeline.context import TickerContext
from app.log_manager import log_manager

logger = logging.getLogger(__name__)


async def run_verify_step(ctx: TickerContext) -> TickerContext:
    """Run hallucination check and inject sufficiency warnings."""
    from app.cycle.orchestration.cycle_control import cycle_control

    # ── Hallucination checker ──
    try:
        from app.pipeline.analysis.hallucination_checker import check_hallucinations

        # Build provenance dict from evidence packet
        context_provenance = {}
        raw_context_parts = []
        for fact in ctx.packet.structured_facts:
            field_name = getattr(fact, "field_name", str(fact))
            field_val = getattr(fact, "value", None)
            source = getattr(fact, "source", "unknown")
            context_provenance[field_name] = {"value": field_val, "source": source}
            raw_context_parts.append(f"{field_name}: {field_val}")

        raw_context = "\n".join(raw_context_parts)

        await cycle_control.wait_if_paused()
        ctx.hallucination_result = check_hallucinations(
            llm_output={
                "action": ctx.final_action,
                "confidence": ctx.final_confidence,
                "rationale": ctx.final_rationale,
            },
            context_provenance=context_provenance,
            raw_context=raw_context,
            ticker=ctx.ticker,
        )

        if ctx.hallucination_result["rejected"]:
            logger.warning(
                "[V2] [HALLUCINATION] %s: REJECTED — %s. Downgrading to HOLD.",
                ctx.ticker, ctx.hallucination_result["rejection_reason"],
            )
            ctx.final_rationale += (
                f"\n\n⚠️ HALLUCINATION GATE REJECTED: "
                f"{ctx.hallucination_result['rejection_reason']}"
            )
            from app.cognition.debate.action_gate import gate_action
            ctx.final_action = gate_action("HOLD", ctx.held)
            ctx.final_confidence = max(10, ctx.final_confidence // 2)
            ctx.safe_emit(
                "analyzing", f"v2_hallucination_{ctx.ticker}",
                f"⚠️ {ctx.ticker}: Hallucination gate REJECTED — "
                f"downgraded to HOLD @ {ctx.final_confidence}%",
                status="warning",
            )
        elif ctx.hallucination_result.get("hallucinations"):
            logger.info(
                "[V2] [HALLUCINATION] %s: %d minor hallucinations (below threshold)",
                ctx.ticker, len(ctx.hallucination_result["hallucinations"]),
            )

        ctx.add_stage("hallucination_check")
        log_manager.log_v2_cycle(ctx.cycle_id, "v2_hallucination_check", {
            "ticker": ctx.ticker,
            "rejected": ctx.hallucination_result.get("rejected", False),
            "hallucination_count": len(ctx.hallucination_result.get("hallucinations", [])),
            "rejection_reason": ctx.hallucination_result.get("rejection_reason", ""),
        })

    except Exception as hall_err:
        logger.warning("[V2] Hallucination check failed for %s (non-fatal): %s", ctx.ticker, hall_err)

    # ── Inject sufficiency warnings + data timeframe into rationale ──
    if ctx.sufficiency and ctx.sufficiency.warnings:
        ctx.final_rationale += f"\n\n⚠️ Data warnings: {'; '.join(ctx.sufficiency.warnings)}"

    if (
        ctx.packet
        and ctx.packet.freshness_summary
        and ctx.packet.freshness_summary.oldest_timestamp
        and ctx.packet.freshness_summary.newest_timestamp
    ):
        oldest_str = ctx.packet.freshness_summary.oldest_timestamp.strftime("%Y-%m-%d %H:%M UTC")
        newest_str = ctx.packet.freshness_summary.newest_timestamp.strftime("%Y-%m-%d %H:%M UTC")
        ctx.final_rationale += f"\n\n📅 Data timeframe: {oldest_str} to {newest_str}"

    mem_brief = ctx.memory_context.get("memory_brief", "")
    if mem_brief and mem_brief != "No prior memory.":
        ctx.final_rationale += f"\n\n📝 Memory context: {mem_brief[:300]}"

    return ctx
