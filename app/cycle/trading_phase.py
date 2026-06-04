
"""
Trading Phase -- Routes decision engine outputs to paper trader.

Takes BUY/SELL/HOLD decisions from the hybrid analysis pipeline
and executes them EXCLUSIVELY through the paper trader. This project does
not support live trading execution by design; all flows are simulated.
"""

import asyncio
import logging
import time
from app.trading.paper_trader import get_portfolio, get_portfolio_value, _get_current_price, buy, sell
from app.services.bot_manager import resolve_bot_id
from app.cycle.orchestration.cycle_control import cycle_control
from app.db.connection import get_db
from app.cycle.attention_tracker import record_trade
from app.pipeline.analysis.outcome_tracker import resolve_outcome
from app.utils.async_utils import run_with_timeout

from app.trading.risk_manager import check_portfolio_constraints
from app.trading.position_sizer import calculate_buy_size

logger = logging.getLogger(__name__)

def estimate_trade(confidence: int, cash: float, current_price: float) -> dict:
    """Estimate shares/$ for a BUY signal without executing.

    Returns: {"size_pct": 7.3, "amount": 7300, "qty": 52, "price": 140.38}
    """
    res = calculate_buy_size(confidence, cash, current_price)
    return {
        "size_pct": res["size_pct"],
        "amount": res["amount"],
        "qty": res["qty"],
        "price": round(current_price, 2),
    }

async def execute_decisions(
    decisions: list[dict],
    bot_id: str = "default",
    cycle_id: str = "",
) -> dict:
    """
    Execute a list of trading decisions using the Execution Agent.
    """
    from app.services.pipeline_service import PipelineService

    start = time.monotonic()
    cid = cycle_id or "no-id"

    bot_id = resolve_bot_id(bot_id)

    logger.info(
        "[PIPELINE] TRADING PHASE START | bot_id=%s | cycle=%s | %d decisions",
        bot_id,
        cid,
        len(decisions),
    )

    portfolio = get_portfolio(bot_id)
    logger.info(
        "[PIPELINE]   Pre-trade portfolio: $%s cash | %d positions | held: %s",
        f"{portfolio.get('cash', 0):,.2f}",
        portfolio.get("position_count", 0),
        [p["ticker"] for p in portfolio.get("positions", [])],
    )

    await cycle_control.wait_if_paused()

    executed = []
    skipped = []

    counts = {
        "holds": 0,
        "human_review": 0,
        "buy_executed": 0,
        "sell_executed": 0,
        "buy_failed": 0,
        "sell_failed": 0,
        "blocked": 0,
        "passes": 0,
        "sell_skipped": 0,
        "crash_fallbacks": 0,
    }

    # Filter out fallbacks and holds before passing to agent
    actionable_decisions = []
    for d in decisions:
        ticker = d.get("ticker", "???")
        action = d.get("action", "HOLD")
        
        if d.get("human_review"):
            skipped.append({"ticker": ticker, "reason": "human_review"})
            counts["human_review"] += 1
            continue

        if d.get("is_timeout_fallback"):
            skipped.append({"ticker": ticker, "reason": "CRASH_FALLBACK"})
            counts["crash_fallbacks"] += 1
            continue
            
        if action == "HOLD":
            counts["holds"] += 1
            continue
            
        actionable_decisions.append(d)

    # ── Dispatch to Deterministic Lego Executor ──
    try:
        from app.services.pipeline_service import PipelineService
        
        # Sort decisions by confidence descending to prioritize highest conviction trades first
        actionable_decisions.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        
        for d in actionable_decisions:
            ticker = d.get("ticker", "UNKNOWN")
            action = d.get("action", "UNKNOWN")
            confidence = d.get("confidence", 0)
            rationale = d.get("rationale", "No rationale provided")
            
            # Refresh portfolio for accurate constraints
            current_portfolio = get_portfolio(bot_id)
            
            # Lego 1: Risk Manager
            is_allowed, block_reason = check_portfolio_constraints(current_portfolio, action)
            if not is_allowed:
                skipped.append({"ticker": ticker, "action": action, "reason": block_reason})
                counts["blocked"] += 1
                logger.info("[TRADING] VETO %s %s: %s", action, ticker, block_reason)
                continue
                
            # Execute Trade
            if action == "BUY":
                # Lego 2: Position Sizer
                current_price = await _get_current_price(ticker)
                sizing = calculate_buy_size(confidence, current_portfolio.get("cash", 0.0), current_price)
                
                if sizing["amount"] <= 0:
                    skipped.append({"ticker": ticker, "action": action, "reason": "Calculated size is 0 (confidence too low or no cash)"})
                    counts["blocked"] += 1
                    continue
                    
                # Call paper trader buy tool natively
                result = await buy(bot_id, ticker, size_pct=sizing["size_pct"] / 100.0)
                if "error" in result:
                    counts["buy_failed"] += 1
                    skipped.append({"ticker": ticker, "action": action, "reason": result["error"]})
                else:
                    counts["buy_executed"] += 1
                    executed.append({
                        "ticker": ticker,
                        "action": "BUY",
                        "size_pct": sizing["size_pct"],
                        "rationale": f"Determined by Lego Sizer: {sizing['size_pct']}% of cash.",
                        "trade_result": result
                    })
                    try:
                        PipelineService.emit("trading", ticker, f"Executed BUY via lego: {rationale}", data=executed[-1])
                        record_trade(ticker)
                    except Exception as emit_err:
                        logger.debug("[TRADE] Failed to emit BUY event: %s", emit_err)

            elif action == "SELL":
                # Sell 100% of position
                result = await sell(bot_id, ticker, qty_pct=1.0)
                if "error" in result:
                    counts["sell_failed"] += 1
                    skipped.append({"ticker": ticker, "action": action, "reason": result["error"]})
                else:
                    counts["sell_executed"] += 1
                    executed.append({
                        "ticker": ticker,
                        "action": "SELL",
                        "size_pct": 100.0,
                        "rationale": "Closed position 100%",
                        "trade_result": result
                    })
                    try:
                        PipelineService.emit("trading", ticker, f"Executed SELL via lego: {rationale}", data=executed[-1])
                        await run_with_timeout(
                            resolve_outcome(ticker, bot_id, cycle_id=cycle_id),
                            timeout=30.0,
                            label=f"resolve_outcome_{ticker}"
                        )
                    except Exception as emit_err:
                        logger.debug("[TRADE] Failed to emit SELL event: %s", emit_err)

    except Exception as e:
        logger.error("[TRADING] Lego execution pipeline failed entirely: %s", e)

    elapsed = time.monotonic() - start
    logger.info(
        "[PIPELINE] TRADING PHASE COMPLETE | "
        "executed=%d (buy=%d, sell=%d) | skipped/blocked=%d | elapsed=%.1fs",
        len(executed),
        counts["buy_executed"],
        counts["sell_executed"],
        len(skipped),
        elapsed,
    )

    return {
        "bot_id": bot_id,
        "executed": executed,
        "skipped": skipped,
        "counts": counts,
        "elapsed_seconds": round(elapsed, 2),
    }

