"""
Pipeline Tools — Exposes key pipeline phases as callable agent tools.

Inspired by Claude Code's 'skills' system: reusable workflows that
agents can invoke as high-level actions instead of being locked into
the rigid pipeline sequence.

This lets the CIO agent say "audit data quality for NVDA" as a tool
call during a debate, instead of waiting for the pipeline to run.
"""

import json
import logging

from app.tools.registry import registry

logger = logging.getLogger(__name__)





# ── Tool 3: Hallucination Check ────────────────────────────────────────
@registry.register(
    name="check_hallucination",
    description=(
        "Run a hallucination check on an LLM claim by cross-referencing it "
        "against the actual data in our database. Useful during debates when "
        "one agent suspects another agent is citing fabricated numbers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker the claim is about.",
            },
            "claim": {
                "type": "string",
                "description": "The specific claim to verify (e.g. 'RSI is 28' or 'P/E ratio is 15.3').",
            },
        },
        "required": ["ticker", "claim"],
    },
    tier=2,
    source="internal_db",
    tags=["hallucination", "verify", "fact-check", "ground-truth"],
)
async def check_hallucination(ticker: str, claim: str) -> str:
    """Verify an LLM claim against ground-truth data in the database."""
    try:
        from app.db.connection import get_db

        with get_db() as db:
            # Gather ground-truth data for comparison
            ground_truth = {}

            # Price data
            try:
                price_row = db.execute(
                    "SELECT close, volume FROM price_history WHERE ticker = %s ORDER BY date DESC LIMIT 1",
                    [ticker],
                ).fetchone()
                if price_row:
                    ground_truth["latest_close"] = price_row[0]
                    ground_truth["latest_volume"] = price_row[1]
            except Exception:
                pass

            # Technical indicators
            try:
                tech_row = db.execute(
                    "SELECT rsi_14, macd, macd_signal, macd_hist, sma_20, sma_50, sma_200, "
                    "atr_14, adx_14, stoch_k, stoch_d, bb_upper, bb_lower "
                    "FROM technicals WHERE ticker = %s ORDER BY date DESC LIMIT 1",
                    [ticker],
                ).fetchone()
                if tech_row:
                    labels = ["rsi_14", "macd", "macd_signal", "macd_hist",
                              "sma_20", "sma_50", "sma_200", "atr_14", "adx_14",
                              "stoch_k", "stoch_d", "bb_upper", "bb_lower"]
                    ground_truth["indicators"] = {
                        labels[i]: tech_row[i] for i in range(len(labels)) if tech_row[i] is not None
                    }
            except Exception:
                pass

            # Fundamentals
            try:
                fund_row = db.execute(
                    "SELECT pe_ratio, market_cap, forward_pe, peg_ratio, price_to_book, "
                    "profit_margin, revenue_growth, debt_to_equity, beta "
                    "FROM fundamentals WHERE ticker = %s ORDER BY snapshot_date DESC LIMIT 1",
                    [ticker],
                ).fetchone()
                if fund_row:
                    fund_labels = ["pe_ratio", "market_cap", "forward_pe", "peg_ratio",
                                   "price_to_book", "profit_margin", "revenue_growth",
                                   "debt_to_equity", "beta"]
                    for i, label in enumerate(fund_labels):
                        if fund_row[i] is not None:
                            ground_truth[label] = fund_row[i]
            except Exception:
                pass

        if not ground_truth:
            return json.dumps(
                {
                    "status": "inconclusive",
                    "ticker": ticker,
                    "claim": claim,
                    "message": "No ground-truth data found for this ticker. Cannot verify.",
                }
            )

        return json.dumps(
            {
                "status": "success",
                "ticker": ticker,
                "claim": claim,
                "ground_truth": ground_truth,
                "instruction": (
                    "Compare the claim against the ground_truth data. "
                    "If the claim cites numbers not present in ground_truth, it may be hallucinated."
                ),
            }
        )
    except Exception as e:
        logger.exception("[PipelineTools] check_hallucination failed for %s", ticker)
        return json.dumps({"status": "error", "ticker": ticker, "message": str(e)})





# ── Tool 7: Search Trading Skills (Lazy-Load Skills) ───────────────────
@registry.register(
    name="search_trading_skills",
    description=(
        "Search and load specific trading skills or sector instructions mid-cycle. "
        "Use this to dynamically fetch expert analysis guidelines for a specific stock or sector."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker to load skills for.",
            },
        },
        "required": ["ticker"],
    },
    tier=1,
    source="internal_db",
    tags=["skills", "instructions", "sector", "strategy"],
)
async def search_trading_skills(ticker: str) -> str:
    """Lazy-load sector-specific trading skills for the agent."""
    try:
        from app.services.trading_skills import load_skill_for_ticker

        skill_content = load_skill_for_ticker(ticker)
        if skill_content:
            return json.dumps(
                {
                    "status": "success",
                    "ticker": ticker,
                    "skill_instructions": skill_content,
                }
            )

        return json.dumps(
            {
                "status": "success",
                "ticker": ticker,
                "message": "No specific trading skills found for this ticker or sector.",
            }
        )
    except Exception as e:
        logger.exception("[PipelineTools] search_trading_skills failed for %s", ticker)
        return json.dumps({"status": "error", "ticker": ticker, "message": str(e)})



# ── Tool 8: Start Trading Cycle ─────────────────────────────────────────
@registry.register(
    name="start_trading_cycle",
    description=(
        "Start a new trading cycle. Use this when the user explicitly asks to run "
        "or start a cycle. You can optionally provide a list of tickers to focus on."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of stock tickers to process. Leave empty to use auto-discovery.",
            },
            "collect": {
                "type": "boolean",
                "description": "Whether to run the data collection phase. Default True.",
            },
            "analyze": {
                "type": "boolean",
                "description": "Whether to run the analysis phase. Default True.",
            },
            "trade": {
                "type": "boolean",
                "description": "Whether to execute trades. Default True.",
            }
        },
    },
    tier=1,
    source="internal_db",
    tags=["pipeline", "start", "cycle", "trigger"],
)
async def start_trading_cycle(tickers: list[str] = None, collect: bool = True, analyze: bool = True, trade: bool = True) -> str:
    """Trigger a new trading cycle by writing a V3 system command."""
    try:
        import uuid
        from app.db.connection import get_db

        job_id = f"job_{uuid.uuid4().hex[:8]}"
        cycle_id = f"cycle-v3-{uuid.uuid4().hex[:8]}"
        payload = {
            "tickers": tickers or [],
            "cycle_id": cycle_id,
        }

        with get_db() as db:
            db.execute(
                "INSERT INTO v3_system_commands (id, command_type, payload, status) VALUES (%s, %s, %s, 'pending')",
                [job_id, "START_V3_CYCLE", json.dumps(payload)],
            )

        logger.info("[PipelineTools] Dispatched V3 cycle command %s (cycle: %s)", job_id, cycle_id)

        return json.dumps({
            "status": "success",
            "message": f"V3 trading cycle started with job ID {job_id}.",
            "job_id": job_id,
            "cycle_id": cycle_id,
        })
    except Exception as e:
        logger.exception("[PipelineTools] start_trading_cycle failed")
        return json.dumps({"status": "error", "message": str(e)})

