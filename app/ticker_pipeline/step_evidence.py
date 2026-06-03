"""
Step: Evidence Packet Build.

Assembles all collected data for a ticker into a structured EvidencePacket
that downstream steps (sufficiency gate, thesis agent, debate) consume.

Extracted from runner.py Step 2.
"""

import asyncio
import logging
import time

from app.ticker_pipeline.context import TickerContext
from app.log_manager import log_manager

logger = logging.getLogger(__name__)


async def run_evidence_step(ctx: TickerContext) -> TickerContext:
    """Build the evidence packet from DB data."""
    from app.cognition.evidence.packet_builder import build_evidence_packet
    from app.cycle.orchestration.cycle_control import cycle_control

    await cycle_control.wait_if_paused()
    t2 = time.monotonic()

    try:
        ctx.packet = await asyncio.wait_for(
            build_evidence_packet(ctx.ticker), timeout=600.0
        )
    except asyncio.TimeoutError:
        logger.error("[V2] Evidence packet build TIMEOUT for %s (600s)", ctx.ticker)
        ctx.safe_emit(
            "analyzing", f"v2_evidence_timeout_{ctx.ticker}",
            f"{ctx.ticker}: Evidence build TIMEOUT", status="error",
        )
        log_manager.log_v2_cycle(ctx.cycle_id, "v2_error", {
            "ticker": ctx.ticker, "error": "Evidence packet build timed out after 600s",
            "error_type": "TimeoutError", "stages_completed": ctx.stages,
            "elapsed_ms": ctx.elapsed_ms(t2),
        })
        raise RuntimeError("Evidence packet build timed out after 600s")

    ms2 = ctx.elapsed_ms(t2)
    ctx.add_stage("evidence_build", ms2)

    # Diagnostic logging
    _teaser = (
        ctx.packet.source_quality_summary.teaser_artifact_risk
        if ctx.packet.source_quality_summary else 0.0
    )
    _diversity = (
        ctx.packet.source_quality_summary.source_diversity
        if ctx.packet.source_quality_summary else 0
    )
    logger.info(
        "[V2] [EVIDENCE] %s: %d claims, %d structured facts, %d sources, "
        "missing=%s, teaser_risk=%.2f, diversity=%d",
        ctx.ticker,
        len(ctx.packet.claims),
        len(ctx.packet.structured_facts),
        len(ctx.packet.source_summaries),
        ctx.packet.missing_fields or "none",
        _teaser, _diversity,
    )
    ctx.safe_emit(
        "analyzing", f"v2_evidence_{ctx.ticker}",
        f"{ctx.ticker}: Evidence packet — {len(ctx.packet.claims)} claims, "
        f"{len(ctx.packet.structured_facts)} facts, "
        f"{len(ctx.packet.missing_fields)} missing, "
        f"teaser_risk={_teaser:.0%}, diversity={_diversity}",
        elapsed_ms=ms2,
    )
    log_manager.log_v2_cycle(ctx.cycle_id, "v2_evidence_build", {
        "ticker": ctx.ticker, "claims": len(ctx.packet.claims),
        "structured_facts": len(ctx.packet.structured_facts),
        "sources": len(ctx.packet.source_summaries),
        "missing_fields": ctx.packet.missing_fields,
        "teaser_risk": _teaser, "diversity": _diversity, "elapsed_ms": ms2,
    })

    return ctx
