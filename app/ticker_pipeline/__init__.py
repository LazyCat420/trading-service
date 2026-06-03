"""
Ticker Pipeline — Lego-brick per-ticker analysis.

Replaces the 1816-line runner.py god object with composable steps.
Each step is its own file with a clean input/output contract:

    ctx = TickerContext(ticker, cycle_id, bot_id, ...)
    ctx = await run_data_step(ctx)
    ctx = await run_evidence_step(ctx)
    ctx = await run_sufficiency_step(ctx)
    ctx = await run_agents_step(ctx)
    ctx = await run_debate_step(ctx)
    ctx = await run_thesis_step(ctx)
    ctx = await run_verify_step(ctx)
    ctx = await run_persist_step(ctx)
    return ctx.to_result()

Each step reads from and writes to the shared TickerContext, making
the data flow explicit and each step independently testable.
"""

from app.ticker_pipeline.context import TickerContext
from app.ticker_pipeline.pipeline import execute_ticker_pipeline

__all__ = ["TickerContext", "execute_ticker_pipeline"]
