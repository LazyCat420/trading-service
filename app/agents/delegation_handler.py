"""
Delegation Handler — Inter-agent follow-up system.

When an agent outputs DELEGATION: @Agent - message, this handler:
1. Parses the delegation target and message
2. Runs a fast mini-response LLM call using the target agent's persona
3. Posts the response to the TaskBoard as a completed investigation
4. Emits a voice event so the UI shows the inter-agent dialogue

Budget cap: max 8 delegation follow-ups per ticker per cycle.
"""

import asyncio
import logging
import re

from app.config.personas import PERSONAS, get_persona_prompt, get_persona_config
from app.services.prism_agent_caller import llm, Priority
from app.services.prism_agent_caller import call_prism_agent
from app.config.config_cognition import LLM_TEMPERATURES

logger = logging.getLogger(__name__)

# Map delegation target names to persona keys
_TARGET_TO_PERSONA = {
    "JANITOR": "DATA_JANITOR",
    "RAY": "DATA_JANITOR",
    "QUANT": "QUANT",
    "ARIS": "QUANT",
    "FUNDAMENTAL": "FUNDAMENTAL",
    "FUNDAMENTALS": "FUNDAMENTAL",
    "PRIYA": "FUNDAMENTAL",
    "SENTIMENT": "BEHAVIORAL",
    "BEHAVIORAL": "BEHAVIORAL",
    "VANCE": "BEHAVIORAL",
    "RISK": "RISK",
    "HELEN": "RISK",
    "PM": "PM",
    "BOSS": "PM",
}

# Track delegation budget per (ticker, cycle_id)
_delegation_counts: dict[str, int] = {}
MAX_DELEGATIONS_PER_TICKER = 8  # Increased from 2 to allow meaningful multi-round debates


def extract_delegation(text: str) -> tuple[str, str] | None:
    """Extract DELEGATION target and message from agent output.

    Returns (target_agent, message) or None if no delegation found.
    """
    if not text:
        return None

    match = re.search(
        r"DELEGATION:\s*@(\w+)(?:\s*[-:]\s*(.+?))?(?:\n|$)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None

    target = match.group(1).strip().upper()
    message = (match.group(2) or "").strip()

    # Skip NONE delegations
    if target == "NONE" or not message:
        return None

    return target, message


async def handle_delegation(
    source_agent: str,
    target_name: str,
    message: str,
    ticker: str,
    cycle_id: str,
    bot_id: str,
    team_context: str = "",
    parent_conversation_id: str | None = None,
    parent_agent_session_id: str | None = None,
) -> str | None:
    """Handle a delegation request from one agent to another.

    Runs a fast mini-response using the target agent's persona,
    posts it to TaskBoard, and emits a voice event.

    Returns the response text or None on failure/budget exceeded.
    """
    # Budget check
    budget_key = f"{ticker}:{cycle_id}"
    current_count = _delegation_counts.get(budget_key, 0)
    if current_count >= MAX_DELEGATIONS_PER_TICKER:
        logger.info(
            "[DELEGATION] Budget exhausted for %s (max %d) — skipping @%s",
            ticker, MAX_DELEGATIONS_PER_TICKER, target_name,
        )
        return None

    # Resolve persona
    persona_key = _TARGET_TO_PERSONA.get(target_name.upper())
    if not persona_key:
        logger.warning(
            "[DELEGATION] Unknown target agent @%s — skipping", target_name,
        )
        return None

    persona_config = get_persona_config(persona_key)
    if not persona_config:
        # Fall back to checking if the key exists in PERSONAS
        if persona_key not in PERSONAS:
            logger.warning(
                "[DELEGATION] No persona config for @%s — skipping", target_name,
            )
            return None
        persona_prompt = PERSONAS[persona_key]["prompt"]
        persona_human_name = PERSONAS[persona_key]["name"]
    else:
        persona_prompt = persona_config.get("system_prompt", get_persona_prompt(persona_key))
        persona_human_name = persona_config.get("name", target_name)

    system_prompt = (
        f"{persona_prompt}\n\n"
        f"A colleague has asked you a specific question. Answer it concisely "
        f"based on the team context below. Keep your response to 2-3 sentences max.\n"
        f"You are responding to a direct request from {source_agent}."
    )

    user_prompt = (
        f"## Request from {source_agent}:\n{message}\n\n"
    )
    if team_context:
        user_prompt += f"## Team Context (TaskBoard findings):\n{team_context}\n\n"
    user_prompt += "Respond concisely to this specific question."

    try:
        response, tokens, ms = await call_prism_agent(
            agent_id=f"CUSTOM_{persona_key}_DELEGATION",
            user_message=user_prompt,
            fallback_system_prompt=system_prompt,
            fallback_agent_name=f"delegation_{persona_key.lower()}",
            temperature=LLM_TEMPERATURES.get("delegation", 0.3),
            max_tokens=8192,
            priority=Priority.LOW,
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            parent_conversation_id=parent_conversation_id,
            parent_agent_session_id=parent_agent_session_id,
        )

        # Increment budget counter
        _delegation_counts[budget_key] = current_count + 1

        logger.info(
            "[DELEGATION] @%s (%s) responded to %s for %s in %dms (%d tokens): %s",
            target_name, persona_human_name, source_agent, ticker,
            ms, tokens or 0, response[:100],
        )

        # Post to TaskBoard
        try:
            from app.agents.task_board import task_board
            await task_board.post_finding(
                source_agent=f"delegation_{persona_key.lower()}",
                content=f"[Response to {source_agent}] {response.strip()[:400]}",
                ticker=ticker,
                cycle_id=cycle_id,
                category="fact",
                confidence=70,
            )
        except Exception as tb_err:
            logger.debug("[DELEGATION] TaskBoard post failed: %s", tb_err)

        # Emit voice event for the responding agent
        try:
            from app.services.agent_voice_service import dispatch_agent_quote
            archetype_map = {
                "DATA_JANITOR": "DATA_JANITOR",
                "QUANT": "QUANT",
                "FUNDAMENTAL": "RESEARCH",
                "BEHAVIORAL": "BULL",
                "RISK": "RISK",
                "PM": "RESEARCH",
            }
            dispatch_agent_quote(
                agent_id=f"DELEGATION_{persona_key}",
                archetype=archetype_map.get(persona_key, "RESEARCH"),
                context={
                    "ticker": ticker,
                    "cycle_id": cycle_id,
                    "tool": "delegation_response",
                    "action_result": "delegation",
                    "quote_override": f"{source_agent} asked: {message[:50]}... → {response.strip()[:80]}",
                },
            )
        except Exception as voice_err:
            logger.debug("[DELEGATION] Voice event failed: %s", voice_err)

        return response.strip()

    except Exception as e:
        logger.error(
            "[DELEGATION] Failed @%s response for %s: %s",
            target_name, ticker, e,
        )
        return None


async def process_delegations_from_findings(
    ticker: str,
    cycle_id: str,
    bot_id: str,
) -> int:
    """Scan recent TaskBoard findings for DELEGATION blocks and handle them.

    Called between MetaOrchestrator stages.
    Returns the number of delegations processed.
    """
    processed = 0

    try:
        from app.agents.task_board import task_board
        findings = await task_board.get_findings(ticker=ticker, cycle_id=cycle_id)

        # Build team context from all findings
        team_context = "\n".join(
            f"- [{f.get('source_agent', '?')}]: {f.get('content', '')[:200]}"
            for f in findings
        )

        for finding in findings:
            content = finding.get("content", "")
            source = finding.get("source_agent", "unknown")

            if source.startswith("delegation_"):
                continue

            delegation = extract_delegation(content)
            if delegation:
                target_name, message = delegation
                
                # Resolve target persona first to check for existing response
                persona_key = _TARGET_TO_PERSONA.get(target_name.upper())
                if not persona_key:
                    continue
                
                # Check if we already responded to this delegation source in this cycle
                response_prefix = f"[Response to {source}]"
                already_responded = False
                for f in findings:
                    if f.get("source_agent") == f"delegation_{persona_key.lower()}" and response_prefix in f.get("content", ""):
                        already_responded = True
                        break
                
                if already_responded:
                    logger.debug(
                        "[DELEGATION] Already responded to @%s from %s in cycle %s — skipping duplicate",
                        target_name, source, cycle_id
                    )
                    continue

                result = await handle_delegation(
                    source_agent=source,
                    target_name=target_name,
                    message=message,
                    ticker=ticker,
                    cycle_id=cycle_id,
                    bot_id=bot_id,
                    team_context=team_context,
                )
                if result:
                    processed += 1

    except Exception as e:
        logger.debug("[DELEGATION] Scan failed for %s: %s", ticker, e)

    if processed > 0:
        logger.info(
            "[DELEGATION] Processed %d delegation(s) for %s in cycle %s",
            processed, ticker, cycle_id,
        )

    return processed


def clear_delegation_budget(ticker: str, cycle_id: str) -> None:
    """Clear the delegation budget counter for a completed cycle."""
    budget_key = f"{ticker}:{cycle_id}"
    _delegation_counts.pop(budget_key, None)
