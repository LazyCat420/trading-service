"""
Step: Ontology Enrichment.

Builds an ontology graph for the ticker (sector relationships, peer links).
Non-blocking — failure is non-fatal and doesn't stop the pipeline.

Extracted from runner.py Step 1.
"""

import asyncio
import logging
import time

from app.ticker_pipeline.context import TickerContext
from app.log_manager import log_manager

logger = logging.getLogger(__name__)


async def run_ontology_step(ctx: TickerContext) -> TickerContext:
    """Build ontology context for the ticker."""
    try:
        from app.cognition.ontology.ontology_builder import OntologyBuilder
        from app.cycle.orchestration.cycle_control import cycle_control

        await cycle_control.wait_if_paused()
        t1 = time.monotonic()
        ctx.ontology_ctx = await asyncio.wait_for(
            OntologyBuilder().execute(ctx.ticker, {"cycle_id": ctx.cycle_id}),
            timeout=15.0,
        )
        
        # ── Evolve graph relationships via Market Simulator ──
        try:
            from app.cognition.ontology.market_simulator import MarketSimulator
            # Run simulation based on newly seeded graph state and news context
            sim_res = await asyncio.wait_for(
                MarketSimulator.simulate_market_opinion(ctx.ticker, agent_name=f"sim_{ctx.ticker}"),
                timeout=45.0
            )
            if sim_res.get("status") == "success":
                logger.info(
                    "[V2] MarketSimulator evolved %d relationships on graph for %s",
                    sim_res.get("relationships_updated", 0), ctx.ticker
                )
        except Exception as sim_err:
            logger.warning("[V2] MarketSimulator simulation failed (non-fatal): %s", sim_err)

        ms1 = ctx.elapsed_ms(t1)
        ctx.add_stage("ontology", ms1)

        node_count = len(ctx.ontology_ctx.get("ontology_nodes", []))
        ctx.safe_emit(
            "analyzing", f"v2_ontology_{ctx.ticker}",
            f"{ctx.ticker}: Ontology built & simulated ({node_count} nodes)",
            elapsed_ms=ms1,
        )
        log_manager.log_v2_cycle(ctx.cycle_id, "v2_ontology", {
            "ticker": ctx.ticker, "node_count": node_count, "elapsed_ms": ms1,
        })
    except Exception as e:
        logger.warning("[V2] Ontology failed for %s (non-fatal): %s", ctx.ticker, e)
        ctx.safe_emit(
            "analyzing", f"v2_ontology_{ctx.ticker}",
            f"{ctx.ticker}: Ontology skipped — {e}",
            status="warning",
        )

    return ctx
