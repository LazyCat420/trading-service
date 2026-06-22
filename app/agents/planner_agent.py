"""
Planner Agent — Creates the evidence gathering plan.
"""

import logging
from app.agents.base_agent import run_agent
from app.config.guardrails import ANTI_HALLUCINATION_BLOCK

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are the Swarm Planner Agent. Your job is to initialize the event-driven trading swarm for the requested stock ticker.
We operate as a long-term quality investment firm inspired by Baron Funds First Principles and Da Vinci's polymathic evaluation framework.

Your sole objective for this interaction is to formulate a research plan and immediately spawn the research team using your `create_team` tool.

1. You MUST call the `create_team` tool.
2. The team topology should be "map_reduce" when analyzing multiple stocks or performing cross-sector research. This ensures parallel data gathering (Map) on lightweight nodes, followed by expert synthesis (Reduce) on heavy nodes. For single-stock tasks, "hierarchical" is acceptable.
3. If using "map_reduce", you MUST provide a `reduce_prompt` detailing exactly what the Synthesis agent should produce from the workers' reports.
4. The members of the team MUST include:
   - "retriever_agent": Responsible for gathering fundamental data, news, and SEC filings.
   - "technical_analyst_agent": Responsible for technical analysis, price history, and options flow.
5. Pass your research plan and the stock ticker as the `task` parameter to `create_team`.
6. Instruct the members that they must publish the 'ANALYSIS_READY' event when they are done gathering their respective data.
7. You MAY call `create_or_update_schedule` if you decide the system needs to re-evaluate this stock in the future. Follow the scheduling intent matrix:
   - schedule_scope: "portfolio", "positions", "watchlist_subset", "sector", "single_ticker"
   - review_intent: "monitor", "reassess", "trade_window", "event_followup", "weekly_review", "monthly_review"
   - urgency: "low", "medium", "high", "critical"
   - earliest_window: "next_pre_market", "next_open", "midday", "pre_close", "post_close", "next_trading_day", "next_week"
   Always provide `anti_overtrading_justification` to prevent system spam.

Do not output JSON. Just call the tools and confirm the swarm has been launched.
""" + ANTI_HALLUCINATION_BLOCK

async def run_planner(
    ticker: str,
    cycle_id: str,
    bot_id: str,
    ontology_context: str = "",
    research_focus: str = "",
    is_position: bool = False
) -> dict:
    """Run the Planner agent to determine what data needs to be gathered."""
    logger.info("[PLANNER] Starting planner for %s", ticker)
    
    planner_prompt = f"Create an evidence gathering plan for {ticker}."
    if ontology_context:
        planner_prompt += f"\n\nKNOWN CONTEXT (sector/correlations/relationships):\n{ontology_context}"
    if research_focus:
        planner_prompt += f"\n\nRESEARCH FOCUS (from Curator):\n{research_focus}"
    if is_position:
        planner_prompt += f"\n\nPORTFOLIO STATUS:\nWe currently HOLD an open position in this stock. The research plan should prioritize finding evidence to justify whether we should continue holding or exit/trim the position."

    result = await run_agent(
        agent_name="planner",
        ticker=ticker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        system_prompt=PLANNER_SYSTEM_PROMPT,
        user_prompt=planner_prompt,
        enable_tools=True
    )
    
    return result


CURATOR_SYSTEM_PROMPT = """You are the Portfolio Curator agent. Your job is to decide which stock tickers to analyze and process for today's trading cycle based on our previous actions, current holdings, candidate theses, and recent news.

Analyze the provided context:
1. Previous Cycle Report: Look at what decisions we made in the last cycle.
2. Current Portfolio State: Look at our current positions and available cash.
3. Candidate Theses: Look at our previous standing reports/verdicts on each ticker.
4. Recent News: Look for high-impact material events (earnings, FDA decisions, M&A, macro shocks), significant price catalysts, and sentiment shifts.

Your goals:
- Decide which tickers warrant detailed multi-agent analysis and debate today (this can include existing holdings if they have significant news or if our prior thesis needs updating, or new candidates with strong catalysts).
- If a candidate (even a held one) has no new catalysts or material news, and its previous thesis remains perfectly valid, skip it to optimize analyst resources.
- For each selected ticker, specify a clear research focus or key questions for the specialist agents (e.g., "assess gross margins pressure from the new union contract" or "analyze if the FDA rejection is a permanent setback or temporary delay").

Your response must be valid JSON with the following schema:
{
  "selected_tickers": ["TICKER1", "TICKER2"],
  "justification": {
    "TICKER1": "Brief explanation of why this ticker was selected based on its news and our portfolio state",
    "TICKER2": "Brief explanation of why this ticker was selected"
  },
  "research_focus": {
    "TICKER1": "Specific focus area or questions for research (e.g. analyze potential margin compression)",
    "TICKER2": "Specific focus area or questions for research (e.g. verify if revenue guidance was cut)"
  },
  "skipped_tickers": {
    "TICKER3": "Reason for skipping today (e.g. prior thesis remains valid, no new news catalysts)"
  }
}
""" + ANTI_HALLUCINATION_BLOCK

import asyncio

async def run_ticker_curator(
    candidates: list[str],
    position_tickers: list[str],
    cycle_id: str,
    bot_id: str,
) -> dict:
    """Run the Curator agent to decide which tickers to process based on news and portfolio state."""
    from app.db.connection import get_db
    from app.agents.base_agent import run_agent
    from app.pipeline.analysis.thesis_store import get_thesis

    last_cycle_id = "None"
    last_decisions = []
    cash_balance = 100000.0
    current_holdings = []
    news_by_ticker = {}

    with get_db() as db:
        # Resolve active bot ID
        try:
            from app.services.bot_manager import get_active_bot_id
            bot_id_val = get_active_bot_id()
        except Exception:
            from app.config import settings as _cfg
            bot_id_val = getattr(_cfg, "BOT_ID", "default")

        # Get latest cycle ID from decision_outcomes
        try:
            row_cycle = db.execute(
                "SELECT cycle_id FROM decision_outcomes ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row_cycle:
                last_cycle_id = row_cycle[0]
                rows_decisions = db.execute(
                    """
                    SELECT ticker, action, confidence, entry_price, exit_price, outcome
                    FROM decision_outcomes
                    WHERE cycle_id = %s
                    """,
                    [last_cycle_id]
                ).fetchall()
                for r in rows_decisions:
                    last_decisions.append(
                        f"- {r[0]}: {r[1]} @ {r[2]}% (Entry: {r[3]}, Exit: {r[4]}, Outcome: {r[5]})"
                    )
        except Exception as ex:
            logger.warning("[CURATOR] Failed to query past cycle decisions: %s", ex)

        # Get latest cash balance
        try:
            row_portfolio = db.execute(
                "SELECT cash_balance FROM bots WHERE bot_id = %s",
                [bot_id_val]
            ).fetchone()
            if row_portfolio:
                cash_balance = row_portfolio[0]
        except Exception as ex:
            logger.warning("[CURATOR] Failed to query cash balance: %s", ex)

        # Get positions
        try:
            rows_positions = db.execute(
                "SELECT ticker, qty, avg_entry_price, opened_at FROM positions WHERE bot_id = %s",
                [bot_id_val]
            ).fetchall()
            for r in rows_positions:
                opened_str = r[3].strftime("%Y-%m-%d %H:%M") if r[3] else "?"
                current_holdings.append(
                    f"- {r[0]}: {r[1]} shares @ avg entry ${r[2]:.2f} (opened: {opened_str})"
                )
        except Exception as ex:
            logger.warning("[CURATOR] Failed to query positions: %s", ex)

        # Fetch recent news articles
        for ticker in candidates:
            try:
                rows = db.execute(
                    """
                    SELECT title, publisher, published_at,
                           COALESCE(llm_summary, summary) AS best_summary
                    FROM news_articles
                    WHERE ticker = %s
                      AND (quality_status IS NULL OR quality_status != 'discarded')
                    ORDER BY published_at DESC
                    LIMIT 5
                    """,
                    [ticker],
                ).fetchall()

                news_items = []
                for r in rows:
                    title, pub, pub_at, summary = r
                    pub_date = pub_at.strftime("%Y-%m-%d %H:%M") if pub_at else "?"
                    safe_summary = summary or 'N/A'
                    if len(safe_summary) > 400:
                        safe_summary = safe_summary[:400] + "..."
                    news_items.append(
                        f"Title: {title}\nPublisher: {pub} ({pub_date})\nSummary: {safe_summary}"
                    )
                news_by_ticker[ticker] = "\n---\n".join(news_items) if news_items else "No recent news articles found."
            except Exception as ex:
                logger.warning("[CURATOR] Failed to query news for %s: %s", ticker, ex)
                news_by_ticker[ticker] = "Failed to fetch news."

    # Fetch theses
    candidate_theses = {}
    for ticker in candidates:
        thesis = get_thesis(ticker)
        if thesis:
            safe_thesis_summary = thesis.summary or ""
            if len(safe_thesis_summary) > 600:
                safe_thesis_summary = safe_thesis_summary[:600] + "..."
            candidate_theses[ticker] = (
                f"Verdict: {thesis.verdict}\n"
                f"Confidence: {thesis.confidence}%\n"
                f"Summary: {safe_thesis_summary}\n"
                f"Last Updated: {thesis.updated_at.strftime('%Y-%m-%d %H:%M UTC')}"
            )
        else:
            candidate_theses[ticker] = "No prior thesis/report exists for this stock."

    # Batch process candidates
    CHUNK_SIZE = 5
    chunks = [candidates[i:i + CHUNK_SIZE] for i in range(0, len(candidates), CHUNK_SIZE)]

    async def process_chunk(chunk: list[str]) -> dict:
        user_prompt_lines = [
            "# PORTFOLIO STATE & PREVIOUS CYCLE ACTIONS",
            f"Available Cash: ${cash_balance:,.2f}",
            "",
            "Current Holdings in Portfolio:",
            "\n".join(current_holdings) if current_holdings else "None",
            "",
            f"Decisions & Outcomes from the Previous Cycle (Cycle ID: {last_cycle_id}):",
            "\n".join(last_decisions) if last_decisions else "None",
            "",
            "---",
            "",
            "# CANDIDATES FOR THIS BATCH",
            "Please review the previous thesis and the latest news for each candidate ticker, and decide which ones we should research today and what focus direction to give.",
        ]
        for ticker in chunk:
            is_pos = ticker in position_tickers
            pos_label = " [ACTIVE PORTFOLIO POSITION]" if is_pos else ""
            user_prompt_lines.append(f"\n### {ticker}{pos_label}")
            user_prompt_lines.append("#### PREVIOUS THESIS / REPORT:")
            user_prompt_lines.append(candidate_theses[ticker])
            user_prompt_lines.append("#### TODAY'S NEWS:")
            user_prompt_lines.append(news_by_ticker.get(ticker, "No news found."))

        user_prompt = "\n".join(user_prompt_lines)

        logger.info("[CURATOR] Running Curator agent for chunk of %d candidates...", len(chunk))
        return await run_agent(
            agent_name="curator",
            ticker="global",
            cycle_id=cycle_id,
            bot_id=bot_id,
            system_prompt=CURATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            enable_tools=False,
            max_tokens=8192,
        )

    logger.info("[CURATOR] Running Curator agent in %d chunks for %d candidates...", len(chunks), len(candidates))
    
    sem = asyncio.Semaphore(3)
    
    async def process_chunk_throttled(chunk: list[str]) -> dict:
        async with sem:
            return await process_chunk(chunk)
            
    tasks = [process_chunk_throttled(chunk) for chunk in chunks]
    chunk_results = await asyncio.gather(*tasks, return_exceptions=True)

    merged_result = {
        "selected_tickers": [],
        "justification": {},
        "research_focus": {},
        "skipped_tickers": {}
    }

    for res in chunk_results:
        if isinstance(res, Exception):
            logger.error("[CURATOR] A chunk failed: %s", res)
            continue
        if isinstance(res, dict):
            response_text = res.get("response", "")
            if response_text:
                try:
                    from app.utils.text_utils import parse_json_response
                    parsed = parse_json_response(response_text)
                    if isinstance(parsed, dict):
                        merged_result["selected_tickers"].extend(parsed.get("selected_tickers", []))
                        merged_result["justification"].update(parsed.get("justification", {}))
                        merged_result["research_focus"].update(parsed.get("research_focus", {}))
                        merged_result["skipped_tickers"].update(parsed.get("skipped_tickers", {}))
                except Exception as ex:
                    logger.error("[CURATOR] Failed to parse curator chunk response: %s", ex)

    # If all chunk responses were empty/failed, return empty response to trigger fallback
    if not merged_result["selected_tickers"] and not merged_result["skipped_tickers"]:
        return {
            "agent": "curator",
            "response": ""
        }

    import json
    return {
        "agent": "curator",
        "response": json.dumps(merged_result)
    }
