"""
Step: Data Completeness + Ticker Processors.

Runs data_completeness.check_and_fill() and data_perticker_collection.run_ticker_processors()
to ensure the evidence packet has clean, summarized data to work with.

Extracted from runner.py Steps 0.5–0.6.
"""

import asyncio
import logging
import time

from app.ticker_pipeline.context import TickerContext
from app.log_manager import log_manager

logger = logging.getLogger(__name__)

# Global semaphore to prevent all 8 workers from flooding LLM queue at once
_DATA_PROCESSOR_SEMAPHORE: asyncio.Semaphore | None = None

def _get_semaphore() -> asyncio.Semaphore:
    global _DATA_PROCESSOR_SEMAPHORE
    if _DATA_PROCESSOR_SEMAPHORE is None:
        from app.config.config import settings
        concurrency = max(4, settings.V2_TICKER_CONCURRENCY)
        _DATA_PROCESSOR_SEMAPHORE = asyncio.Semaphore(concurrency)
    return _DATA_PROCESSOR_SEMAPHORE


async def run_data_step(ctx: TickerContext) -> TickerContext:
    """Check data completeness and run ticker processors."""

    # ── Data Completeness ──
    t0 = time.monotonic()
    try:
        from app.pipeline.data.data_completeness import check_and_fill

        ctx.data_report = await asyncio.wait_for(
            check_and_fill(ctx.ticker, emit=ctx.emit), timeout=30.0
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[V2] Data completeness TIMEOUT for %s (30s) — proceeding without gap fill",
            ctx.ticker,
        )
        ctx.data_report = {}
    except Exception as e:
        logger.warning("[V2] Data completeness FAILED for %s: %s", ctx.ticker, e)
        ctx.data_report = {}

    ms0 = ctx.elapsed_ms(t0)
    filled = ctx.data_report.get("filled", [])
    if filled:
        logger.info("[V2] [DATA] Filled gaps for %s: %s", ctx.ticker, filled)
        ctx.safe_emit(
            "analyzing", f"v2_data_{ctx.ticker}",
            f"{ctx.ticker}: Filled {len(filled)} data gaps",
            data={"filled": filled}, elapsed_ms=ms0,
        )
    ctx.add_stage("data_completeness", ms0)
    log_manager.log_v2_cycle(ctx.cycle_id, "v2_data_completeness", {
        "ticker": ctx.ticker, "filled": filled, "elapsed_ms": ms0,
    })

    # ── Ticker Processors (Smart Janitor, Summarizer, Consensus, Narrative) ──
    t_proc = time.monotonic()
    try:
        # Check if price history has 0 rows, if so, skip data processors and flag the report
        price_count = ctx.data_report.get("available", {}).get("price_history", 0)
        if price_count == 0:
            from app.db.connection import get_db
            try:
                with get_db() as db:
                    price_count = db.execute(
                        "SELECT COUNT(*) FROM price_history WHERE ticker = %s", [ctx.ticker]
                    ).fetchone()[0]
            except Exception:
                price_count = 0

        if price_count == 0:
            logger.warning("[V2] %s has 0 price history rows. Skipping data processors.", ctx.ticker)
            if "available" not in ctx.data_report:
                ctx.data_report["available"] = {}
            ctx.data_report["available"]["price_history"] = 0
            ctx.add_stage("ticker_processors", ctx.elapsed_ms(t_proc))
            return ctx

        from app.pipeline.data.data_perticker_collection import run_ticker_processors

        ctx.safe_emit(
            "analyzing", f"v2_processors_{ctx.ticker}",
            f"{ctx.ticker}: Waiting for data processor slot...",
            status="running",
        )
        
        sem = _get_semaphore()
        async with sem:
            ctx.safe_emit(
                "analyzing", f"v2_processors_{ctx.ticker}",
                f"{ctx.ticker}: Running data processors (Smart Janitor, Summarizer, Consensus, Narrative)...",
                status="running",
            )
            await asyncio.wait_for(
                run_ticker_processors(ctx.ticker, ctx.emit), timeout=300.0
            )
            
        ms_proc = ctx.elapsed_ms(t_proc)
        ctx.add_stage("ticker_processors", ms_proc)
        logger.info("[V2] Data processors completed for %s in %dms", ctx.ticker, ms_proc)

        # ── Emit Agent Voice Quote ──
        try:
            from app.services.agent_voice_service import dispatch_agent_quote
            dispatch_agent_quote(
                agent_id="DATA_JANITOR_AGENT",
                archetype="DATA_JANITOR",
                context={
                    "ticker": ctx.ticker,
                    "cycle_id": ctx.cycle_id,
                    "tool": "data_processors",
                    "action_result": "complete",
                }
            )
        except Exception as voice_err:
            logger.debug("Voice event trigger failed: %s", voice_err)
    except Exception as e:
        logger.warning("[V2] Data processors failed for %s (non-fatal): %s", ctx.ticker, e)

    return ctx
