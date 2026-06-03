"""
Step: Sufficiency Gate + Dynamic Retrieval.

Checks whether the evidence packet has enough data to make a trading decision.
If critical gaps exist, triggers one round of dynamic retrieval to fill them.
If gaps persist after retrieval, returns an ABSTAIN result.

Extracted from runner.py Steps 3-4.5.
"""

import asyncio
import logging
import time
from typing import Any

from app.ticker_pipeline.context import TickerContext
from app.core.result_builder import build_v1_compatible_result
from app.log_manager import log_manager

logger = logging.getLogger(__name__)


async def run_sufficiency_step(ctx: TickerContext) -> TickerContext | dict[str, Any] | None:
    """Check data sufficiency and attempt retrieval if needed.

    Returns:
        TickerContext if pipeline should continue.
        dict if ABSTAIN (result ready to return).
        None if ticker should be rejected entirely (fake/delisted).
    """
    from app.cognition.verification.sufficiency_gate import check_data_sufficiency
    from app.cycle.orchestration.cycle_control import cycle_control

    ctx.sufficiency = check_data_sufficiency(ctx.ticker, ctx.packet)
    ctx.add_stage("sufficiency_gate")
    log_manager.log_v2_cycle(ctx.cycle_id, "v2_sufficiency_gate", {
        "ticker": ctx.ticker, "status": ctx.sufficiency.status,
        "blockers": ctx.sufficiency.blockers if hasattr(ctx.sufficiency, "blockers") else [],
        "warnings": ctx.sufficiency.warnings if hasattr(ctx.sufficiency, "warnings") else [],
    })

    # ── Dynamic retrieval (max 1 retry) ──
    if ctx.sufficiency.status == "critical_gap" and ctx.packet.missing_fields:
        ctx.safe_emit(
            "analyzing", f"v2_retrieval_{ctx.ticker}",
            f"{ctx.ticker}: Critical gap detected — triggering dynamic retrieval "
            f"for {ctx.packet.missing_fields}",
            status="running",
        )
        try:
            from app.pipeline.analysis.dynamic_tool_router import resolve_missing_data
            from app.cognition.evidence.packet_builder import build_evidence_packet

            await cycle_control.wait_if_paused()
            t_r = time.monotonic()
            fetched = await resolve_missing_data(ctx.ticker, ctx.packet.missing_fields)
            ms_r = ctx.elapsed_ms(t_r)

            if fetched:
                ctx.retrieval_retries = 1
                ctx.packet = await build_evidence_packet(ctx.ticker)
                ctx.sufficiency = check_data_sufficiency(ctx.ticker, ctx.packet)
                ctx.safe_emit(
                    "analyzing", f"v2_retrieval_done_{ctx.ticker}",
                    f"{ctx.ticker}: Retrieval done, re-evaluated "
                    f"sufficiency → {ctx.sufficiency.status}",
                    elapsed_ms=ms_r,
                )
            ctx.add_stage("dynamic_retrieval")
        except Exception as e:
            logger.warning("[V2] Dynamic retrieval failed for %s: %s", ctx.ticker, e)
            ctx.safe_emit(
                "analyzing", f"v2_retrieval_fail_{ctx.ticker}",
                f"{ctx.ticker}: Dynamic retrieval failed — {e}",
                status="warning",
            )

    # ── If still critical, ABSTAIN ──
    if ctx.sufficiency.status == "critical_gap":
        return await _handle_abstain(ctx)

    return ctx


async def _handle_abstain(ctx: TickerContext) -> dict[str, Any] | None:
    """Handle critical gap — either reject the ticker or return HOLD."""
    from app.cognition.debate.action_gate import gate_action

    blockers_text = "; ".join(ctx.sufficiency.blockers)
    logger.warning("[V2] ABSTAIN for %s — critical gaps remain: %s", ctx.ticker, blockers_text)

    # Check if missing critical price data → fake or delisted ticker
    if "price" in ctx.packet.missing_fields or "Missing critical price history data." in ctx.sufficiency.blockers:
        logger.warning("[V2] %s is missing critical price data — auto-rejecting", ctx.ticker)
        try:
            from app.processors.ticker_extractor import (
                get_registry as _get_reg_yf,
                _save_rejected_to_db as _reject_db,
                FALSE_TICKERS as _FT,
            )
            from app.db.connection import get_db

            _reg_yf = _get_reg_yf()
            _reg_yf.add_rejected(ctx.ticker)
            _FT.add(ctx.ticker)
            _reject_db(ctx.ticker)

            with get_db() as db:
                db.execute("DELETE FROM watchlist WHERE ticker = %s", [ctx.ticker])
        except Exception as rej_err:
            logger.debug("[V2] auto-reject write failed for %s: %s", ctx.ticker, rej_err)

        ctx.safe_emit(
            "analyzing", f"v2_reject_{ctx.ticker}",
            f"{ctx.ticker}: THROWN OUT — missing critical price data",
            status="error",
        )
        return None  # Phase 4 drops it entirely

    ctx.safe_emit(
        "analyzing", f"v2_abstain_{ctx.ticker}",
        f"{ctx.ticker}: ABSTAIN — {blockers_text}",
        status="warning",
    )

    abstain_result = build_v1_compatible_result(
        ticker=ctx.ticker,
        action=gate_action("HOLD", ctx.held),
        confidence=0,
        rationale=f"V2 ABSTAIN: Insufficient evidence. {blockers_text}",
        cycle_id=ctx.cycle_id,
        total_tokens=0,
        elapsed=ctx.elapsed_s(),
        stages=ctx.stages,
        config_used="v2_abstain",
    )

    # Log abstain decision to DB
    try:
        from app.core.db_writer import log_decision

        if ctx.db_semaphore:
            async with ctx.db_semaphore:
                log_decision(abstain_result, ctx.cycle_id, ctx.bot_id)
        else:
            log_decision(abstain_result, ctx.cycle_id, ctx.bot_id)
        ctx.add_stage("db_log")
    except Exception as e:
        logger.warning("[V2] log_decision (abstain) failed for %s: %s", ctx.ticker, e)

    try:
        from app.cycle.orchestration.post_cycle_hooks import run_post_cycle_hooks

        await run_post_cycle_hooks(
            ticker=ctx.ticker, result=abstain_result, escalated=False,
            cycle_id=ctx.cycle_id,
            final_action=gate_action("HOLD", ctx.held),
            final_confidence=0,
        )
        ctx.add_stage("post_cycle_hooks")
    except Exception as e:
        logger.warning("[V2] post_cycle_hooks (abstain) failed for %s: %s", ctx.ticker, e)

    return abstain_result
