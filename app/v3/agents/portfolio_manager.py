"""
Watchlist Gatekeeper — ACTIVE, but not run by the V3 orchestrator.

PipelineService imports SYSTEM_PROMPT/AGENT_NAME from here and runs this
agent (tools disabled, strict JSON) as the watchlist Gatekeeper that selects
which scored candidate tickers get a full V3 pipeline run. It does NOT read
or manage portfolio state — live portfolio reads happen via the
get_portfolio_state tool (quant/board agents) and in paper_trader.

The previous docstring said "INACTIVE — never invoked"; that was wrong and
nearly got this module deleted during the 2026-07-15 dead-code sweep.
"""

AGENT_NAME = "v3_portfolio_manager"

ARTIFACT_TYPE = "portfolio_screener"

TOOL_WHITELIST = [
    "get_finnhub_news",
    "lazy_web_search",
    "get_market_data",
    # Research pipeline management: the gatekeeper owns the research budget —
    # schedule/queue the best candidates, prune stale ones (governor-capped).
    "get_upcoming_events",
    "list_scheduled_research",
    "schedule_research",
    "request_research_now",
    "cancel_scheduled_research",
    # Watch Desk: leave cheap "wake me if…" watch conditions so the desk keeps
    # monitoring a name in code without burning a cycle until a trigger trips.
    "watch_ticker",
    "list_watches",
    "clear_watch",
    # Parameter governance: read the live risk limits; propose standard-tier
    # changes (sizing caps, thresholds, budgets) through the governor.
    "get_parameters",
    "propose_parameter_change",
]

SYSTEM_PROMPT = """You are the Portfolio Gatekeeper.

You will receive a list of stocks that passed our Freshness Gate — each one has been verified to have either new data or material changes worth analyzing.

Your job: Select which stocks to send to deep analysis. Pick between {min_tickers} and {max_tickers} from the list.

## RULES
1. MAXIMUM ONE MEGA-CAP: Only 1 of AAPL, MSFT, GOOGL, NVDA, AMZN per cycle.
2. VERIFY CATALYSTS: Check that the volume/trend signal has a logical catalyst backing it.
3. EMBRACE VOLATILITY: Prefer explosive setups and momentum shifts over safe baseline stocks.
4. BALANCE SOURCES: Mix trending discoveries (Reddit, News) with watchlist setups.
5. NEVER select 0 — if you received this list, there are stocks worth analyzing.
6. RISK ENVELOPE: `get_parameters` shows the live runtime limits (sizing caps,
   research/watch budgets, thresholds). If a budget or cap is genuinely
   constraining good work, `propose_parameter_change` with specific evidence —
   the governor clamps, cools down, and auto-reverts loosening changes.

## OUTPUT
Output ONLY a JSON object. No conversational text, no markdown blocks.
{
  "selected_tickers": ["TICKER1", "TICKER2"],
  "rationale": "Brief 1-sentence reasoning for the selection."
}
"""
