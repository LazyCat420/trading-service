"""
Base agent pattern — every agent follows this exact structure.

Phase 2: Agents receive pre-computed data from processors.
Phase 3: Optional dynamic meta-prompt generates context-aware system prompts.
LLM only analyzes — never calculates.
"""

import datetime
import logging

from app.config import settings

from app.utils.text_utils import parse_json_response, sanitize_ascii
from app.utils.resilience import aresilient_call

logger = logging.getLogger(__name__)

# ─── Meta-prompt: generates a context-aware system prompt ───────────
AGENT_META_SYSTEM = """You are an expert at creating specialized analyst system prompts for stock market analysis.

Given an agent's role description and a preview of the market data, create an IMPROVED system prompt tailored to THIS specific analysis.

STRICT GUARDRAILS — you MUST follow these:
1. PRESERVE the exact JSON output schema from the original prompt (same keys, same value types)
2. The generated prompt must ONLY instruct the agent to analyze the data it receives — never tell it to fetch, search, or hallucinate data
3. Keep the prompt under 200 words — concise prompts produce better LLM output
4. Include "Respond with JSON:" followed by the exact schema from the original prompt
5. NEVER remove the instruction "do NOT recalculate" or "the data given is authoritative"

WHAT TO ADAPT based on the data preview:
- Identify the asset class: blue-chip stock, growth stock, penny stock, crypto, commodity, ETF
- For PENNY STOCKS (price < $5): emphasize liquidity risk, dilution risk, and pump-and-dump patterns
- For CRYPTO (BTC/ETH/XRP): skip P/E and fundamentals, focus on momentum and sentiment cycles
- For BLUE CHIPS: emphasize macro sensitivity, dividend sustainability, institutional positioning
- Reference specific data patterns you see (e.g., "RSI is oversold" or "revenue declining")
- Name the sector/industry if identifiable from the ticker or data

Respond with ONLY JSON:
{"system_prompt": "the full improved system prompt with JSON schema preserved", "focus_rationale": "1 sentence on what you adapted and why"}"""

AGENT_META_USER = """## Agent Role: {agent_name}

## Original System Prompt (template — preserve its JSON output schema exactly):
{static_prompt}

## Data Preview (first 8000 chars of what the agent will analyze):
{data_preview}

---

Create a better, more specific system prompt for this agent. You MUST preserve the exact JSON output schema from the original prompt. Adapt the analytical focus to what matters most for this specific ticker and data."""


# _parse_json_response moved to app.utils.text_utils.parse_json_response
_parse_json_response = parse_json_response

# ── Agents that receive prior trade outcome context ──
_OUTCOME_CONTEXT_AGENTS = frozenset({
    "sentiment", "technical", "fundamental", "risk", "fund_flow",
    "comparative", "retriever",
})


def get_ticker_outcome_context(ticker: str) -> str:
    """Pull resolved trade outcomes for this ticker from the DB.

    Returns a formatted string for analyst prompt injection,
    or empty string if no history exists.  Queries PostgreSQL
    (decision_outcomes table) — deterministic, bounded, no flat-file I/O.
    """
    if not ticker or ticker.startswith("_"):
        return ""  # Skip synthetic tickers like _AUDIT_
    try:
        from app.pipeline.analysis.outcome_tracker import get_past_outcomes

        outcomes = get_past_outcomes(ticker=ticker, limit=5)
        if not outcomes:
            return ""

        lines = [f"\n## PRIOR TRADE HISTORY FOR {ticker}"]
        for o in outcomes:
            lines.append(
                f"- {o['outcome']}: entry=${o.get('entry_price', 0):.2f} → "
                f"exit=${o.get('exit_price', 0):.2f} ({o.get('pnl_pct', 0):+.1f}%) "
                f"conf={o.get('confidence', 0)} [{o.get('resolved_at', '?')}]"
            )
        lines.append(
            "Use this history to calibrate your confidence — "
            "do not repeat past mistakes.\n"
        )
        return "\n".join(lines)
    except Exception:
        return ""





async def run_agent(
    agent_name: str,
    ticker: str,
    cycle_id: str,
    bot_id: str,
    system_prompt: str,
    user_prompt: str,
    data_context: str = "",
    temperature: float = 0.3,
    max_tokens: int = 1024,
    endpoint_override: str | None = None,
    enable_tools: bool = False,
    response_format: dict | None = None,
    parent_conversation_id: str | None = None,
    parent_agent_session_id: str | None = None,
    model_override: str | None = None,
) -> dict:
    """
    Generic agent runner:
    1. Optionally generate a dynamic system prompt via meta-prompt
    2. Inject data_context (pre-computed signals) into user prompt
    3. Call llm.chat() with monitoring metadata
    4. Return structured result dict

    Every specific agent builds its own prompts and calls this.
    """
    # ── V3 relies on specialized static prompts and no DB queries ──

    # ── Inject prior trade outcome context for analysis agents ──
    outcome_ctx = ""
    if agent_name in _OUTCOME_CONTEXT_AGENTS:
        outcome_ctx = get_ticker_outcome_context(ticker)

    # ── Budget-aware data truncation ──
    # Prevent any single component from blowing the context window
    from app.config.context_budget import get_context_budget

    ctx_budget = get_context_budget()

    # Inject shared whiteboard state before truncation
    try:
        from app.agents.whiteboard import whiteboard
        board_context = await whiteboard.summarize(ticker, cycle_id)
        if board_context:
            data_context = f"{board_context}\n\n{data_context}" if data_context else board_context
    except Exception as e:
        logger.error("[BaseAgent] Failed to fetch whiteboard context: %s", e)

    if data_context and len(data_context) > ctx_budget.data_context_chars:
        original_len = len(data_context)
        data_context = data_context[: ctx_budget.data_context_chars]
        logger.info(
            "[BaseAgent] %s data_context truncated: %d -> %d chars (budget=%d)",
            agent_name,
            original_len,
            len(data_context),
            ctx_budget.data_context_chars,
        )

    # Inject pre-computed data before the analysis request
    if data_context:
        full_prompt = f"{outcome_ctx}{data_context}\n\n{user_prompt}"
    else:
        full_prompt = f"{outcome_ctx}{user_prompt}" if outcome_ctx else user_prompt
        


    # ── Verbose input logging ──
    prompt_label = "STATIC"
    print(f"\n  {'~' * 50}")
    print(f"  AGENT INPUT: {agent_name} ({ticker}) [{prompt_label} PROMPT]")
    print(f"  {'~' * 50}")
    print(f"  System Prompt ({len(system_prompt)} chars):")
    safe_sys = sanitize_ascii(system_prompt)
    print(f"    {safe_sys}")
    print(f"  User Prompt ({len(full_prompt)} chars):")
    safe_user = sanitize_ascii(full_prompt)
    print(f"    {safe_user}")
    print(f"  {'~' * 50}")

    @aresilient_call(retries=3, backoff="exponential", base_delay=1.0, max_delay=15.0)
    async def _agent_llm_call():
        from app.agents.tool_whitelists import get_agent_tools, get_agent_budget_turns

        # Per-agent tool whitelist: only show tools relevant to this agent's role
        agent_tools = get_agent_tools(agent_name) if enable_tools else []

        # Per-agent turn budget: reasoning-only agents get 1, tool agents get role-specific limits
        max_turns = get_agent_budget_turns(agent_name, enable_tools)

        # Agent loop using lazycat-sdk
        from lazycat.agent import BaseAgent, AgentHarness
        from lazycat.session import ConversationSession
        import time
        from lazycat.llm import prism_client

        # ── Tool Loop Detection ──
        # Prevents agents from calling the same failing tool endlessly.
        # The detector tracks (tool_name, args_hash, status) and blocks
        # calls that have failed 3+ times with identical arguments.
        from app.v3.guardrails import ToolLoopDetector
        loop_detector = ToolLoopDetector(max_identical_failures=3)

        tool_call_count = 0

        def _on_tool_call(tool_name: str, arguments: dict) -> str | None:
            """Pre-call hook: check if this tool+args combo should be blocked."""
            nonlocal tool_call_count
            tool_call_count += 1
            # Check if this exact combo has already been blocked
            check_result = loop_detector.record_call(tool_name, arguments, failed=True)
            if check_result is not None:
                return check_result
            # Undo the speculative failure record — actual outcome will be recorded in _on_tool_result
            key = loop_detector._make_key(tool_name, arguments, failed=True)
            loop_detector._history[key] = max(0, loop_detector._history.get(key, 1) - 1)
            return None

        def _on_tool_result(tool_name: str, arguments: dict, result, was_blocked: bool) -> None:
            """Post-call hook: record the actual outcome."""
            if was_blocked:
                return  # Already recorded as failure in pre-check
            # Determine if the tool call failed
            failed = False
            if isinstance(result, dict):
                if result.get("error") or result.get("is_error"):
                    failed = True
                elif not result:
                    failed = True
            elif result is None:
                failed = True
            loop_detector.record_call(tool_name, arguments, failed=failed)

        from app.services.prism_agent_registry import resolve_agent_id
        prism_agent_id = resolve_agent_id(agent_name)
        
        kwargs = {
            "name": prism_agent_id, 
            "system_prompt": system_prompt,
            "llm_client": prism_client,
            "project": settings.PROJECT_NAME
        }
        if model_override:
            kwargs["model"] = model_override
            
        agent = BaseAgent(**kwargs)
        if enable_tools and agent_tools:
            for t in agent_tools:
                agent.add_tool(t)

        session = ConversationSession(session_id=parent_agent_session_id or f"sess_{int(time.time())}")
        
        from app.agents.inbox import inbox_manager
        inbox_manager.register_instance(session.session_id, agent_name, ticker)
        
        try:
            harness = AgentHarness(
                agent=agent,
                session=session,
                max_iterations=max_turns,
                on_tool_call=_on_tool_call if enable_tools else None,
                on_tool_result=_on_tool_result if enable_tools else None,
            )

            t0 = time.time()
            final_text = await harness.run(full_prompt)
            elapsed_ms = int((time.time() - t0) * 1000)
        finally:
            inbox_manager.unregister_instance(session.session_id)

        return (
            final_text,
            0,  # Token usage not tracked by base SDK yet
            elapsed_ms,
            tool_call_count + 1,
        )

    content, tokens, elapsed_ms, loops_used = await _agent_llm_call()

    if not content or not str(content).strip():
        content = f"Agent failed: empty response from {agent_name}"

    # ── Verbose output logging ──
    print(f"\n  {'~' * 50}")
    print(f"  AGENT OUTPUT: {agent_name} ({ticker}) [{tokens} tokens, {elapsed_ms}ms]")
    print(f"  {'~' * 50}")
    safe_content = sanitize_ascii(content) if content else ""
    print(f"    {safe_content}")
    print(f"  {'~' * 50}")

    return {
        "agent": agent_name,
        "ticker": ticker,
        "cycle_id": cycle_id,
        "bot_id": bot_id,
        "response": content,
        "tokens_used": tokens,
        "execution_ms": elapsed_ms,
        "loops_used": loops_used,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }
