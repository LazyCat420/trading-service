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

# RUNTIME: the PM is invoked with `enable_tools=False` (pipeline_service.py
# ~line 1128, "DISABLED tools so it strictly outputs JSON") and records ZERO
# tool calls in 60 days. That makes this list look like dead config, and it has
# been proposed for deletion twice.
#
# DO NOT DELETE IT. `prism_registration` reads `module.TOOL_WHITELIST` directly
# and passes it as the persona's `availableTools`. An EMPTY availableTools does
# not mean "no tools" to Prism — it means UNSCOPED, i.e. full-catalog discovery
# headroom (observed live on CUSTOM_V3_DECISION_SYNTHESIZER 2026-07-22; that is
# how agents reached execute_command/write_file before the meta-tool lockdown).
# The non-empty list is what keeps the registered persona scoped.
# Pinned by tests/unit/test_tool_whitelist_enforcement.py.
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
