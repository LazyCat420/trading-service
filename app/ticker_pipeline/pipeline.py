"""
Pipeline — Main entry point that chains all Lego steps.

This replaces the 1350-line execute_v2_pipeline() function from runner.py
with a clean 80-line orchestrator that calls each step in sequence.

Each step is independently testable and debuggable.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable

from app.ticker_pipeline.context import TickerContext
from app.ticker_pipeline.step_cache import run_cache_step
from app.ticker_pipeline.step_data import run_data_step
from app.ticker_pipeline.step_ontology import run_ontology_step
from app.ticker_pipeline.step_evidence import run_evidence_step
from app.ticker_pipeline.step_sufficiency import run_sufficiency_step
from app.ticker_pipeline.step_memory import run_memory_step
from app.ticker_pipeline.step_agents import run_agents_step
from app.ticker_pipeline.step_debate import run_debate_step
from app.ticker_pipeline.step_thesis import run_thesis_step
from app.ticker_pipeline.step_verify import run_verify_step
from app.ticker_pipeline.step_persist import run_persist_step
from app.log_manager import log_manager

logger = logging.getLogger(__name__)

# ── Chart task registry (shared with runner.py during migration) ──
_chart_tasks: set[asyncio.Task] = set()


def drain_chart_tasks() -> list[asyncio.Task]:
    """Return and clear all tracked chart tasks for cancellation."""
    tasks = list(_chart_tasks)
    _chart_tasks.clear()
    return tasks


async def execute_ticker_pipeline(
    ticker: str,
    *,
    cycle_id: str = "",
    bot_id: str = "",
    emit: Callable[..., Any] | None = None,
    macro_memo: str = "",
    watchlist: list[str] | None = None,
    db_semaphore: asyncio.Semaphore | None = None,
    thesis_semaphore: asyncio.Semaphore | None = None,
    is_highly_redundant: bool = False,
) -> dict[str, Any] | None:
    """Run the full V2 cognition pipeline for a single ticker.

    This is the composable replacement for runner.py's execute_v2_pipeline().
    Returns a dict with the same keys as V1's analyze_ticker() so the
    trading phase, post-cycle hooks, and report generation work unchanged.
    """
    from app.utils.pipeline_utils import noop as _noop
    from app.cycle.orchestration.cycle_control import cycle_control

    if emit is None:
        emit = _noop

    ticker = ticker.upper()
    if not cycle_id:
        cycle_id = f"v2-{uuid.uuid4().hex[:8]}"

    # ── Build context ──
    ctx = TickerContext(
        ticker=ticker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        emit=emit,
        macro_memo=macro_memo,
        watchlist=watchlist or [],
        db_semaphore=db_semaphore,
        thesis_semaphore=thesis_semaphore,
        is_highly_redundant=is_highly_redundant,
    )

    ctx.safe_emit(
        "analyzing", f"v2_start_{ticker}",
        f"{ticker}: V2 cognition pipeline starting",
        status="running",
    )

    # ── Pause check ──
    await cycle_control.wait_if_paused()

    # ── Fetch position context early ──
    try:
        from app.tools.portfolio_tools import get_position_context
        ctx.position_context = get_position_context(ticker, bot_id)
    except Exception as e:
        logger.debug("[V2] Position context query failed for %s: %s", ticker, e)
    ctx.held = ctx.position_context.get("held", False)

    try:
        from app.tools.portfolio_tools import get_portfolio_risk_dashboard
        ctx.portfolio_dashboard = get_portfolio_risk_dashboard(ticker, bot_id)
    except Exception as e:
        logger.debug("[V2] Portfolio risk dashboard failed for %s: %s", ticker, e)

    log_manager.log_v2_cycle(cycle_id, "v2_start", {
        "ticker": ticker, "held": ctx.held,
        "position_context": {k: v for k, v in ctx.position_context.items() if k != "raw"} if ctx.position_context else {},
    })

    # ── Launch chart generation in background ──
    try:
        from app.agents.technical_analyst_agent import run_technical_analyst

        async def _run_chart():
            try:
                success = await run_technical_analyst(ticker=ticker, cycle_id=cycle_id, bot_id=bot_id)
                if success:
                    logger.info("[V2] Pre-generated trading chart for %s", ticker)
                else:
                    logger.warning("[V2] Chart generation failed for %s", ticker)
            except asyncio.CancelledError:
                logger.info("[V2] Chart generation cancelled for %s", ticker)
            except Exception as e:
                logger.warning("[V2] Chart generation failed for %s: %s", ticker, e)

        task = asyncio.create_task(_run_chart())
        _chart_tasks.add(task)
        task.add_done_callback(_chart_tasks.discard)
    except Exception as e:
        logger.warning("[V2] Failed to initiate chart generation: %s", e)

    # ── Fast-track for held positions ──
    if ctx.held:
        from app.cognition.orchestration.runner import execute_open_position_fast_track
        return await execute_open_position_fast_track(
            ticker=ticker, cycle_id=cycle_id, bot_id=bot_id,
            emit=emit, macro_memo=macro_memo,
            position_context=ctx.position_context,
            portfolio_dashboard=ctx.portfolio_dashboard,
            thesis_semaphore=thesis_semaphore,
            db_semaphore=db_semaphore,
            start_time=ctx.start_time,
        )

    # ══════════════════════════════════════════════════════════════════
    #  LEGO PIPELINE — Each step is its own file, independently testable
    # ══════════════════════════════════════════════════════════════════

    ctx = await run_cache_step(ctx)
    if ctx.fast_track_cache:
        # Cache hit! Skip the heavy lifting and jump straight to persist.
        # But first run ontology to keep it fresh (non-blocking)
        asyncio.create_task(run_ontology_step(ctx))
        return await run_persist_step(ctx)

    # Step 0.5: Data completeness + processors
    logger.info("[V2] %s: Step 0.5 - Data Completeness", ticker)
    ctx = await run_data_step(ctx)

    # Step 1: Ontology enrichment
    logger.info("[V2] %s: Step 1 - Ontology", ticker)
    ctx = await run_ontology_step(ctx)

    # Step 2: Evidence packet build
    logger.info("[V2] %s: Step 2 - Evidence Build", ticker)
    ctx = await run_evidence_step(ctx)

    # Step 3-4: Sufficiency gate + dynamic retrieval
    logger.info("[V2] %s: Step 3 - Sufficiency Gate", ticker)
    result_or_ctx = await run_sufficiency_step(ctx)
    if result_or_ctx is None:
        return None  # Ticker rejected (fake/delisted)
    if isinstance(result_or_ctx, dict):
        return result_or_ctx  # ABSTAIN result

    # Step 5: Memory context injection
    logger.info("[V2] %s: Step 5 - Memory Context", ticker)
    ctx = await run_memory_step(ctx)

    # Step 5.5: MetaOrchestrator agent routing + team findings
    logger.info("[V2] %s: Step 5.5 - Agents Routing", ticker)
    ctx = await run_agents_step(ctx)

    # Step 5.7: Adversarial debate
    logger.info("[V2] %s: Step 5.7 - Debate", ticker)
    ctx = await run_debate_step(ctx)

    # Step 6: Thesis generation
    logger.info("[V2] %s: Step 6 - Thesis Generation", ticker)
    ctx = await run_thesis_step(ctx)

    # Step 6.5: Hallucination check + rationale enrichment
    logger.info("[V2] %s: Step 6.5 - Verification", ticker)
    ctx = await run_verify_step(ctx)

    # Steps 7-11: Persist to DB, memory, hooks, attention
    logger.info("[V2] %s: Step 7 - Persist", ticker)
    result = await run_persist_step(ctx)

    return result
