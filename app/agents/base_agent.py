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

_active_agents = set()

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
    prism_overrides: dict | None = None,
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

    # Inject pre-computed data into the SYSTEM prompt to prevent Prism from embedding it.
    # Prism's workflow-query embeds user messages; if data_context > 2048 tokens, the embedding model crashes.
    if data_context or outcome_ctx:
        system_prompt = f"{system_prompt}\n\n[PRE-COLLECTED DATA CONTEXT]\n{outcome_ctx}{data_context}"
    full_prompt = user_prompt
        


    # ── Verbose input logging (structured, truncated to prevent log spam) ──
    _sys_preview = sanitize_ascii(system_prompt[:500])
    _user_preview = sanitize_ascii(full_prompt[:1000])
    logger.debug(
        "[BaseAgent] INPUT %s (%s) | sys_prompt=%d chars | user_prompt=%d chars\n"
        "  System: %s%s\n  User: %s%s",
        agent_name, ticker, len(system_prompt), len(full_prompt),
        _sys_preview, "..." if len(system_prompt) > 500 else "",
        _user_preview, "..." if len(full_prompt) > 1000 else "",
    )

    @aresilient_call(retries=3, backoff="exponential", base_delay=1.0, max_delay=15.0)
    async def _agent_llm_call():
        from app.agents.tool_whitelists import get_agent_tools, get_agent_budget_turns

        # Extract overrides from Settings panel
        overrides = prism_overrides or {}
        domain_blocklist = overrides.get("tool_domain_blocklist", [])

        # Per-agent tool whitelist: only show tools relevant to this agent's role
        agent_tools = get_agent_tools(agent_name, domain_blocklist=domain_blocklist) if enable_tools else []

        # Per-agent turn budget: reasoning-only agents get 1, tool agents get role-specific limits
        max_turns = get_agent_budget_turns(agent_name, enable_tools)

        # Agent loop using lazycat-sdk
        from lazycat.agent import BaseAgent, AgentHarness
        from lazycat.session import ConversationSession
        import time
        from lazycat.llm import prism_client

        # NOTE: prism_client.url is set ONCE per cycle in PipelineService._run_all_v3()
        # to prevent a race condition where concurrent agent calls stomp on the global
        # singleton URL. Do NOT override prism_client.url here.


        t0 = time.time()
        tool_call_count = 0
        prior_calls = []

        def _on_tool_result(tool_name: str, arguments: dict, result, was_blocked: bool, elapsed_ms: int = 0) -> None:
            """Post-call hook: record the actual outcome to V3 telemetry."""
            nonlocal tool_call_count
            tool_call_count += 1
            
            failed = False
            error_msg = ""
            if was_blocked:
                failed = True
                error_msg = "Blocked by ToolLoopDetector"
            elif isinstance(result, str):
                if result.startswith("Error:") or "Exception" in result:
                    failed = True
                    error_msg = result[:500]
            elif isinstance(result, dict):
                if result.get("error") or result.get("is_error"):
                    failed = True
                    error_msg = str(result.get("error", result.get("message", "")))[:500]
                elif not result:
                    failed = True
                    error_msg = "Empty result"
            elif result is None:
                failed = True
                error_msg = "None result"

            final_tool_name = tool_name
            provider = None
            if tool_name == "lazy_web_search" and not failed:
                try:
                    import json
                    res_dict = result if isinstance(result, dict) else json.loads(result)
                    provider = res_dict.get("provider")
                    if provider:
                        final_tool_name = f"lazy_web_search_{provider.lower()}"
                except Exception:
                    pass

            try:
                from app.v3.tool_telemetry import record_tool_call, _hash_args
                record_tool_call(
                    cycle_id=cycle_id,
                    agent_name=agent_name,
                    tool_name=final_tool_name,
                    args_hash=_hash_args(arguments),
                    success=not failed,
                    was_blocked=was_blocked,
                    error_message=error_msg,
                    elapsed_ms=elapsed_ms,
                    ticker=ticker,
                )
                
                if provider:
                    try:
                        from app.telemetry.bus import publish_event
                        from app.telemetry.schema import TelemetryEvent
                        from datetime import datetime, timezone
                        publish_event(TelemetryEvent(
                            ts=datetime.now(timezone.utc).isoformat(),
                            cycle_id=cycle_id,
                            ticker=ticker,
                            kind="pipeline",
                            source="agent_tool",
                            status="ok",
                            step=f"{provider.lower()}_{ticker}",
                            phase="collecting",
                            detail=f"Web search via {provider}",
                        ))
                    except Exception as ev_err:
                        logger.debug(f"Failed to emit web search telemetry: {ev_err}")
            except Exception as e:
                logger.debug(f"Telemetry failed: {e}")

            # Doom loop check
            current_call = {"name": tool_name, "args": arguments, "error": error_msg}
            prior_calls.append(current_call)

            # Check 1: Tool loop repetition (same tool + args >= 3 times)
            same_calls = [c for c in prior_calls if c["name"] == tool_name and c["args"] == arguments]
            if len(same_calls) >= 3:
                from app.services.streaming_observer import DoomLoopException
                logger.error(
                    "[ManagerAgent] Caught tool doom loop for %s: repeating %s with %s",
                    agent_name, tool_name, arguments
                )
                raise DoomLoopException(f"Agent {agent_name} caught in tool doom loop calling {tool_name} 3 times.")

            # Check 2: Error loop repetition (same tool + same error >= 3 times)
            if failed and error_msg:
                same_errors = [c for c in prior_calls if c["name"] == tool_name and c["error"] == error_msg]
                if len(same_errors) >= 3:
                    from app.services.streaming_observer import DoomLoopException
                    logger.error(
                        "[ManagerAgent] Caught tool error doom loop for %s on %s: %s",
                        agent_name, tool_name, error_msg
                    )
                    raise DoomLoopException(f"Agent {agent_name} caught in tool error loop for {tool_name}: {error_msg}")

            # Check 3: Active session time limit check
            elapsed_s = time.time() - t0
            if elapsed_s > 180 and tool_call_count > 4:
                from app.services.streaming_observer import DoomLoopException
                logger.error(
                    "[ManagerAgent] Agent %s took too much time (%.1fs) over %d tool turns without completing.",
                    agent_name, elapsed_s, tool_call_count
                )
                raise DoomLoopException(f"Agent {agent_name} exceeded active progress time limit (elapsed: {elapsed_s:.1f}s, turns: {tool_call_count})")

        from app.services.prism_agent_registry import resolve_agent_id
        prism_agent_id = resolve_agent_id(agent_name)
        
        kwargs = {
            "name": prism_agent_id, 
            "system_prompt": system_prompt,
            "llm_client": prism_client,
            "project": settings.PROJECT_NAME,
            "username": settings.PRISM_USERNAME,
            "auto_approve": overrides.get("prism_auto_approve", True),
        }
        resolved_model = model_override
        resolved_provider = None
        if not resolved_model:
            from app.services.prism_agent_caller import resolve_default_model_for_agent
            try:
                resolved_model, resolved_provider = await resolve_default_model_for_agent(agent_name)
                logger.info("[BaseAgent] Dynamically resolved default model for %s: %s (provider: %s)", agent_name, resolved_model, resolved_provider)
            except Exception as e:
                logger.warning("[BaseAgent] Failed to resolve default model for %s: %s. Using default fallback.", agent_name, e)
        
        if resolved_model:
            kwargs["model"] = resolved_model
        if resolved_provider:
            kwargs["provider"] = resolved_provider
            
        agent = BaseAgent(**kwargs)
        if enable_tools and agent_tools:
            for t in agent_tools:
                agent.add_tool(t)

        import uuid
        session = ConversationSession(session_id=parent_agent_session_id or f"sess_{int(time.time())}_{uuid.uuid4().hex[:6]}")
        
        _active_agents.add(agent_name)
        
        try:
            harness = AgentHarness(
                agent=agent,
                session=session,
                max_iterations=max_turns,
                on_tool_result=_on_tool_result if enable_tools else None,
            )

            t0 = time.time()
            final_text = await harness.run(full_prompt)
            elapsed_ms = int((time.time() - t0) * 1000)
        finally:
            _active_agents.discard(agent_name)

        return (
            final_text,
            0,  # Token usage not tracked by base SDK yet
            elapsed_ms,
            tool_call_count + 1,
        )

    content, tokens, elapsed_ms, loops_used = await _agent_llm_call()

    if not content or not str(content).strip():
        content = f"Agent failed: empty response from {agent_name}"

    # ── Verbose output logging (structured, truncated to prevent log spam) ──
    _out_preview = sanitize_ascii(content[:1500]) if content else ""
    logger.debug(
        "[BaseAgent] OUTPUT %s (%s) | %d tokens | %dms | response=%d chars\n  %s%s",
        agent_name, ticker, tokens, elapsed_ms, len(content) if content else 0,
        _out_preview, "..." if content and len(content) > 1500 else "",
    )

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
