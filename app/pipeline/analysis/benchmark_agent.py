"""
Benchmark Agent — Post-cycle strategy evaluator.

Analyzes the bot's trading performance over recent cycles using the
get_performance_metrics tool, compares it against the active Trading
Constitution, and proposes amendments via the propose_constitution_amendment
tool if the performance justifies a change.
"""

import json
import logging

from app.services.vllm_client import llm, Priority
from app.services.prism_agent_caller import call_prism_agent
from app.config import settings

logger = logging.getLogger(__name__)

# System prompt is now loaded dynamically by Prism from app/agents/custom/benchmark_agent.py


async def run_benchmark_agent(cycle_id: str, days_back: int = 30) -> dict:
    """Run the benchmark agent to evaluate performance and potentially propose amendments.

    Returns a dict with the outcome of the agent's evaluation.
    """
    logger.info(
        "[BENCHMARK] Starting post-cycle performance evaluation (days_back=%d)",
        days_back,
    )

    # We fetch the current constitution so the agent knows what the rules are
    from app.pipeline.trading_constitution import format_constitution_for_prompt

    constitution_block = format_constitution_for_prompt()

    # Fetch live performance metrics to inject as context
    perf_context = ""
    try:
        from app.trading.paper_trader import get_portfolio

        portfolio = get_portfolio(settings.BOT_ID)
        perf_context = (
            f"\nCURRENT PORTFOLIO:\n"
            f"  Cash: ${portfolio.get('cash', 0):,.2f}\n"
            f"  Positions: {portfolio.get('position_count', 0)}\n"
            f"  Total PnL: ${portfolio.get('total_pnl', 0):,.2f}\n"
        )
    except Exception as e:
        logger.debug("[BENCHMARK] Failed to fetch portfolio: %s", e)

    # ── Fetch real trade performance from decision_outcomes (ground truth) ──
    trade_perf = ""
    try:
        from app.db.connection import get_db

        with get_db() as db:
            # Win rate and PnL stats from resolved outcomes
            stats_row = db.execute(
                """
                SELECT
                    COUNT(*) as total,
                    COUNT(CASE WHEN outcome = 'WIN' THEN 1 END) as wins,
                    COUNT(CASE WHEN outcome = 'LOSS' THEN 1 END) as losses,
                    COUNT(CASE WHEN outcome = 'FLAT' THEN 1 END) as flats,
                    COALESCE(AVG(CASE WHEN outcome = 'WIN' THEN pnl_pct END), 0) as avg_win,
                    COALESCE(AVG(CASE WHEN outcome = 'LOSS' THEN pnl_pct END), 0) as avg_loss,
                    COALESCE(AVG(pnl_pct), 0) as avg_pnl
                FROM decision_outcomes
                WHERE resolved_at IS NOT NULL
                  AND outcome != 'CANCELED'
                  AND resolved_at > CURRENT_TIMESTAMP - INTERVAL '%s days'
                """ % days_back,
            ).fetchone()

            if stats_row and stats_row[0] > 0:
                total, wins, losses, flats = stats_row[0], stats_row[1], stats_row[2], stats_row[3]
                win_rate = (wins / total * 100) if total > 0 else 0
                trade_perf = (
                    f"\nTRADE PERFORMANCE (last {days_back} days):\n"
                    f"  Total closed trades: {total}\n"
                    f"  Win rate: {win_rate:.1f}% ({wins}W / {losses}L / {flats}F)\n"
                    f"  Avg win: +{stats_row[4]:.2f}%\n"
                    f"  Avg loss: {stats_row[5]:.2f}%\n"
                    f"  Overall avg PnL: {stats_row[6]:.2f}%\n"
                )

            # Recent lot closures for context
            closures = db.execute(
                """
                SELECT ticker, realized_pnl, holding_days, closed_at
                FROM lot_closures
                WHERE closed_at > CURRENT_TIMESTAMP - INTERVAL '%s days'
                ORDER BY closed_at DESC LIMIT 10
                """ % days_back,
            ).fetchall()

            if closures:
                trade_perf += "\n  RECENT CLOSED POSITIONS:\n"
                for c in closures:
                    trade_perf += f"    {c[0]}: ${c[1]:+,.2f} PnL, held {c[2] or '?'} days\n"

    except Exception as e:
        logger.debug("[BENCHMARK] Failed to fetch trade performance: %s", e)

    user_prompt = (
        f"Cycle {cycle_id} has completed.\n\n"
        f"CURRENT CONSTITUTION:\n"
        f"{constitution_block or 'No active constitution rules.'}\n\n"
        f"{perf_context}\n"
        f"{trade_perf}\n"
        f"Please evaluate our performance over the last {days_back} days. "
        f"Analyze the data and determine if any constitution amendments are needed."
    )

    try:
        response, tokens, elapsed_ms = await call_prism_agent(
            agent_id="CUSTOM_BENCHMARK_AGENT",
            user_message=user_prompt,
            fallback_system_prompt="",
            fallback_agent_name="benchmark_agent",
            temperature=0.2,
            max_tokens=2048,
            priority=Priority.LOW,
            cycle_id=cycle_id,
        )

        logger.info("[BENCHMARK] Agent responded (%d tokens, %dms)", tokens, elapsed_ms)

        # If the LLM returned raw JSON
        if isinstance(response, str) and response.strip().startswith("{"):
            try:
                parsed = json.loads(response)
                return parsed
            except json.JSONDecodeError:
                pass

        return {"status": "completed", "llm_output": response}

    except Exception as e:
        logger.error("[BENCHMARK] Agent failed: %s", e)
        return {"status": "error", "message": str(e)}
