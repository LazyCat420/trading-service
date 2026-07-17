import logging

logger = logging.getLogger(__name__)

# Per-block cap for memory context (~4 chars/token → ~1.5k tokens each) so the
# brain-graph + working-memory blocks combined can't balloon the context budget.
_MEMORY_BLOCK_MAX_CHARS = 6000


def _cap(text: str, max_chars: int = _MEMORY_BLOCK_MAX_CHARS) -> str:
    """Truncate a memory block to a char budget, appending an elision marker."""
    if text and len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n… [truncated]"
    return text


def _build_retrieved_context(ticker: str) -> str:
    """Semantic recall over the embedded corpus (news / analysis / graph-claims)
    via the hybrid retriever (dense + BM25 + RRF). Returns an empty string when
    nothing relevant is found or on any failure — always non-fatal."""
    try:
        from app.services.retrieval_hybrid import hybrid_retriever

        chunks = hybrid_retriever.retrieve(
            ticker, f"{ticker} latest analysis news catalysts outlook", top_k=6
        )
    except Exception as e:
        logger.debug("[RLM] Retrieved context failed (non-fatal): %s", e)
        return ""

    if not chunks:
        return ""

    lines = [
        "========================================",
        f"## Retrieved Context [{ticker}] (semantic recall)",
        "========================================",
    ]
    for c in chunks:
        snippet = (c.content or "").strip().replace("\n", " ")[:280]
        lines.append(f"- [{c.source_table} · {c.score:.2f}] {snippet}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Compact system prompt -- custom tools FIRST, llm_query demoted to fallback
# ---------------------------------------------------------------------------
TRADING_SYSTEM_PROMPT = """You are a trading analyst with REPL access. Use ```repl``` code blocks to query data and make a BUY/SELL/HOLD decision.

IMPORTANT: You have direct database query tools. ALWAYS use these first -- do NOT parse the raw context string manually.
All tools auto-print their return values. Just call them directly (no logger.info() needed).

## Primary Tools (ALWAYS START HERE):
{custom_tools_section}

## Step-by-step workflow:
1. FIRST call: get_technicals(ticker) and get_fundamentals(ticker)
2. THEN call: get_sentiment(ticker) and get_latest_price(ticker)
3. OPTIONALLY: get_congress(ticker), get_institutional(ticker), get_market_regime()
4. Analyze the returned data -- compute ratios, compare signals
5. LEARN: Call graph_learn() if you discover cross-ticker relationships (e.g. two tickers driven by same catalyst), sector themes, or causal chains (e.g. rate cuts → bank margins → financials).
6. Call FINAL with your decision JSON

## Fallback tools (only if primary tools return errors or missing data):
- `context` -- raw market data string (large, unstructured -- avoid if tools work)
- `llm_query(prompt)` -- ask a sub-LLM to analyze text (slow, use only as last resort)
- `SHOW_VARS()` -- list REPL variables
- `trigger_deep_research(ticker, cycle_id)` -- Trigger mid-cycle data collection if critical data (e.g. fundamentals, news) is missing.
- `search_trading_skills(ticker)` -- Dynamically load expert analysis instructions for a stock or sector.
- `create_team(name, members, topology="map_reduce", reduce_prompt="...")` -- Delegate parallel research (Map on Jetson) followed by synthesis (Reduce on Gold Spark).
- `amend_constitution(...)` -- (DESTRUCTIVE) Propose changes to risk/position limits if current rules are failing.

## Output format:
FINAL({{"action": "BUY", "confidence": 75, "rationale": "RSI=37.8 oversold, PE=22.1 reasonable, revenue +15% YoY"}})

Action must be BUY, SELL, HOLD, or PASS. Confidence 0-100. Cite specific numbers from tool outputs.

GROUNDING REQUIREMENT: Your rationale MUST reference specific data values from the tools.
Name the exact indicator values (e.g., "RSI=37.8", "PE=22.1", "revenue $6.2B +15% YoY").
Quote at least 3-5 specific numbers from tool outputs in your rationale.
Generic statements like "technicals are bullish" without citing numbers will be flagged by the audit system.

SELL RULE: You may ONLY recommend SELL if the ticker appears in your CURRENT PORTFOLIO section.
If you do not hold a position in this ticker, your options are BUY or PASS. You cannot HOLD a stock you do not own; use PASS instead.
Issuing a SELL or HOLD for a ticker you don't own will fail execution."""


ESCALATION_SYSTEM_PROMPT = """You are a senior trading escalation agent. The primary baseline check failed due to low confidence or conflicting signals. You must conduct a deep-dive analysis.

IMPORTANT: You are running in recursion-enabled mode (max_depth=2). You have the ability to explicitly spawn child LLMs to read unstructured text data.

## Escalation Protocol:
{custom_tools_section}

1. Gather raw data using primary tools (technicals, fundamentals, sentiment, latest price).
2. For complex or conflicting unstructured data across multiple stocks, use `create_team` with "map_reduce" topology to delegate parallel web searches. Otherwise use "hierarchical" or `llm_query` for single queries.
3. If critical data is completely missing, use `trigger_deep_research` to fill gaps mid-cycle.
4. Compare the signals. You must resolve the conflict that caused the initial low confidence.
5. Call FINAL with your heavily vetted decision JSON.

## Output format:
FINAL({{"action": "BUY", "confidence": 85, "rationale": "Base check hesitated, but deep dive reveals..."}})

Action must be BUY, SELL, HOLD, or PASS. Confidence 0-100. Cite specific numbers from tool outputs.

GROUNDING REQUIREMENT: Your rationale MUST reference specific data values from the tools.
Name the exact indicator values (e.g., "RSI=37.8", "PE=22.1", "revenue $6.2B +15% YoY").
Quote at least 3-5 specific numbers from tool outputs in your rationale.
Generic statements without citing numbers will be flagged by the audit system."""


def build_rlm_prompt(
    ticker: str,
    is_escalation: bool = False,
    system_prompt_override: str | None = None,
    bot_id: str = "",
    retrieved_override: str | None = None,
) -> str:
    """Builds the complete RLM system prompt including memory, skills, and portfolio.

    retrieved_override: a precomputed "Retrieved Context" block (e.g. the
    decomposed-recall block built in the async escalation path). When provided,
    it replaces the default single-query hybrid retrieval block.
    """
    prompt_parts = []

    # Memory context = brain-graph activation AND the 5-store working memory,
    # combined (not either/or). Previously working memory was only a fallback
    # that almost never ran, so prospective/procedural/semantic/episodic memory
    # never reached the LLM. Each block is capped so the sum stays bounded.
    memory_blocks: list[str] = []
    if ticker:
        try:
            from app.cognition.ontology.ontology_builder import BrainGraph

            graph_ctx = BrainGraph.get_activated_context(ticker)
            if graph_ctx and graph_ctx.strip():
                memory_blocks.append(_cap(graph_ctx))
        except Exception as graph_err:
            logger.debug("[RLM] Graph context failed (non-fatal): %s", graph_err)

        try:
            from app.services.memory.working_memory import working_memory

            wm_ctx = working_memory.get_context(ticker)
            # get_context always returns header scaffolding; only inject when it
            # actually carries content (a "### " section = reminders/facts/etc).
            if wm_ctx and "### " in wm_ctx:
                memory_blocks.append(_cap(wm_ctx))
        except Exception as wm_err:
            logger.debug("[RLM] Working memory failed (non-fatal): %s", wm_err)

    if memory_blocks:
        prompt_parts.append("\n\n".join(memory_blocks))

    # Semantic recall over the embedded corpus (Phase 3 — needs embedding_ingest
    # to have populated news/analysis/graph_claims). Capped + non-fatal. When the
    # caller precomputed a decomposed-recall block (escalation path), use that.
    if ticker:
        retrieved_block = retrieved_override or _build_retrieved_context(ticker)
        if retrieved_block:
            prompt_parts.append(_cap(retrieved_block))

    if ticker:
        from app.services.trading_skills import load_skill_for_ticker

        skill_block = load_skill_for_ticker(ticker)
        if skill_block:
            prompt_parts.append(skill_block)

    try:
        from app.trading.paper_trader import get_portfolio
        from app.config import settings as _pf_settings

        bot_id_for_pf = getattr(_pf_settings, "BOT_ID", "default")
        portfolio_data = get_portfolio(bot_id_for_pf)
        if portfolio_data:
            held_tickers = [p["ticker"] for p in portfolio_data.get("positions", [])]
            portfolio_block = (
                "# CURRENT PORTFOLIO STATE\n"
                f"Cash: ${portfolio_data.get('cash', 0):,.2f}\n"
                f"Open positions: {len(held_tickers)}\n"
                f"Tickers held: {', '.join(held_tickers) if held_tickers else 'None'}\n"
            )
            if ticker in held_tickers:
                portfolio_block += f"You HOLD {ticker} — SELL or HOLD are valid options.\n"
            else:
                portfolio_block += (
                    f"You do NOT hold {ticker} — SELL is NOT valid. Only BUY or PASS.\n"
                )
            prompt_parts.append(portfolio_block)
    except Exception as pf_err:
        logger.debug("[RLM] Portfolio injection failed (non-fatal): %s", pf_err)

    if system_prompt_override:
        sp_template = system_prompt_override
    else:
        sp_template = (
            ESCALATION_SYSTEM_PROMPT if is_escalation else TRADING_SYSTEM_PROMPT
        )
    prompt_parts.append(sp_template)

    # Inject Custom Bot Profile Constraints
    try:
        from app.services.bot_manager import get_bot_description

        bot_desc = get_bot_description(bot_id)
        if bot_desc:
            bot_desc_block = (
                "## CUSTOM BOT TRADING INSTRUCTIONS\n"
                f"{bot_desc}\n\n"
                "(You must implicitly follow the above instructions in your decision and rationale. "
                "These rules override any baseline constraints unless mathematically impossible.)"
            )
            # Insert this before the system prompt template so it acts as an overarching constraint
            prompt_parts.insert(0, bot_desc_block)
    except Exception as desc_err:
        logger.debug("[RLM] Bot description injection failed (non-fatal): %s", desc_err)

    return "\n\n".join(prompt_parts)
