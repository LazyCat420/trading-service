"""
Planner Agent — Creates the evidence gathering plan.
"""

import logging
from app.agents.base_agent import run_agent

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are the Planner agent. Your job is to create a structured evidence-gathering checklist for a trading decision on the requested stock.
You must output a JSON object containing a detailed plan.
The plan MUST cover the following categories:
1. Fundamentals (P/E ratio, revenue growth, profit margins, balance sheet health, earnings history)
2. Technicals (RSI, Moving Averages, price trends, volume trends, support/resistance levels)
3. News Sentiment (recent earnings call highlights, press releases, social media sentiment, macro context)
4. Flows (institutional holdings, insider trades, options order flow)

Your response must be valid JSON with the following schema:
{
  "ticker": "string",
  "categories": {
    "fundamentals": ["list of specific data points / metrics to fetch"],
    "technicals": ["list of specific technical indicators to fetch"],
    "sentiment": ["list of news / sentiment sources to analyze"],
    "flows": ["list of flow metrics to look up"]
  },
  "justification": "Why this specific plan is tailored to the stock (e.g. growth vs value, sector factors)"
}
"""

async def run_planner(
    ticker: str,
    cycle_id: str,
    bot_id: str,
    ontology_context: str = ""
) -> dict:
    """Run the Planner agent to determine what data needs to be gathered."""
    logger.info("[PLANNER] Starting planner for %s", ticker)
    
    planner_prompt = f"Create an evidence gathering plan for {ticker}."
    if ontology_context:
        planner_prompt += f"\n\nKNOWN CONTEXT (sector/correlations/relationships):\n{ontology_context}"

    result = await run_agent(
        agent_name="planner",
        ticker=ticker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        system_prompt=PLANNER_SYSTEM_PROMPT,
        user_prompt=planner_prompt,
        enable_tools=False
    )
    
    return result


CURATOR_SYSTEM_PROMPT = """You are the Portfolio Curator agent. Your job is to decide which stock tickers to analyze and process for today's trading cycle based on their recent news and headlines.
You are given a list of candidate tickers, their recent news, and whether they represent an active position in the portfolio.

Analyze the news for each ticker. Look for:
1. High-impact material events (earnings releases, product launches, clinical trials, regulatory approvals/rejections, M&A activity, major guidance revisions).
2. Significant market/price catalysts, high volatility, or strong news sentiment shifts.
3. For active positions: whether there is news that requires auditing/updating our position strategy.

You must decide which tickers warrant full detailed analysis today. You can select all, some, or none of them. Be selective to optimize analyst resources, focusing on the most actionable and material opportunities.

Your response must be valid JSON with the following schema:
{
  "selected_tickers": ["TICKER1", "TICKER2"],
  "justification": {
    "TICKER1": "Brief explanation of why this ticker was selected based on its news",
    "TICKER2": "Brief explanation of why this ticker was selected based on its news"
  },
  "skipped_tickers": {
    "TICKER3": "Brief explanation of why this ticker was skipped (e.g., no material news, low volatility, etc.)"
  }
}
"""

async def run_ticker_curator(
    candidates: list[str],
    position_tickers: list[str],
    cycle_id: str,
    bot_id: str,
) -> dict:
    """Run the Curator agent to decide which tickers to process based on news."""
    from app.db.connection import get_db
    from app.agents.base_agent import run_agent

    news_by_ticker = {}
    with get_db() as db:
        for ticker in candidates:
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
                news_items.append(
                    f"Title: {title}\nPublisher: {pub} ({pub_date})\nSummary: {summary or 'N/A'}"
                )

            news_by_ticker[ticker] = "\n---\n".join(news_items) if news_items else "No recent news articles found."

    user_prompt_lines = [
        "Please review the news for the following candidate tickers and decide which ones to process for the day.",
        "Candidate Tickers:",
    ]
    for ticker in candidates:
        is_pos = ticker in position_tickers
        pos_label = " [ACTIVE PORTFOLIO POSITION]" if is_pos else ""
        user_prompt_lines.append(f"\n### {ticker}{pos_label}")
        user_prompt_lines.append(news_by_ticker[ticker])

    user_prompt = "\n".join(user_prompt_lines)

    logger.info("[CURATOR] Running Curator agent for %d candidates...", len(candidates))
    result = await run_agent(
        agent_name="curator",
        ticker="global",
        cycle_id=cycle_id,
        bot_id=bot_id,
        system_prompt=CURATOR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        enable_tools=False,
        max_tokens=4096,
    )

    return result
