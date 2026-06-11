"""
Data Integrity Agent — Ray's pipeline role.

Validates the EvidencePacket for data quality issues BEFORE other specialist
agents run. Posts findings to the TaskBoard so downstream agents (Priya, Vance,
Aris, Helen) are aware of data risks.

This is NOT the database-cleanup janitor (janitor_agent.py). This is Ray's
cognitive role: skeptical data validation during live analysis.
"""

import logging
import re

from app.config.personas import get_persona_prompt
from app.services.vllm_client import llm, Priority
from app.services.prism_agent_caller import call_prism_agent
from app.config.config_cognition import LLM_TEMPERATURES
from app.cognition.contracts.evidence import EvidencePacket
from app.config.guardrails import ANTI_HALLUCINATION_BLOCK, PEER_ACCOUNTABILITY_BLOCK, DATA_MISSING_PROTOCOL

logger = logging.getLogger(__name__)

CONCLUSION_RULES_BLOCK = """

[INTERACTION & CONCLUSION RULES]
1. You are on a strict time budget. Do not pontificate.
2. You must conclude your analysis within 2 paragraphs.
3. You may tag ONE other agent to request a follow-up (e.g., "@Quant: Double-check your moving averages").
4. You MUST end your output with a definitive stance using this exact format:
   - STANCE: [CLEAN / DIRTY / SUSPECT]
   - CONFIDENCE: [1-100]
   - DELEGATION: [Tag another agent or "NONE"]
"""

DATA_INTEGRITY_SYSTEM_PROMPT = (
    get_persona_prompt("DATA_JANITOR") + "\n\n"
    "You are running a PRE-ANALYSIS data integrity check. Your job is to inspect "
    "the structured facts in the evidence packet and flag any data quality issues.\n\n"
    "CHECK FOR:\n"
    "1. MISSING DATA: Are critical fields (price, volume, PE ratio, revenue) absent?\n"
    "2. STALE DATA: Is the latest price data older than 2 trading days?\n"
    "3. VOLUME ANOMALIES: Any volume spikes > 3x the average that could indicate bad data or stock splits?\n"
    "4. PRICE DISCONTINUITIES: Sudden 50%+ price changes that weren't explained by splits or earnings?\n"
    "5. CONTRADICTORY SOURCES: Does news sentiment contradict price action in a suspicious way?\n"
    "6. MISSING CANDLES: Gaps in OHLCV data that shouldn't exist?\n\n"
    "If the data looks clean, say so. If there are issues, LIST THEM specifically.\n"
    "Warn the team which agents should be careful (e.g., 'Quant, your moving averages may be wrong').\n\n"
    "Output exactly this JSON:\n"
    "{\n"
    '  "data_health": "CLEAN|DIRTY|SUSPECT",\n'
    '  "issues": ["issue 1", "issue 2"],\n'
    '  "warnings": ["warning for specific agent"],\n'
    '  "summary": "1-2 sentence data health assessment"\n'
    "}\n"
    + CONCLUSION_RULES_BLOCK
    + ANTI_HALLUCINATION_BLOCK + PEER_ACCOUNTABILITY_BLOCK + DATA_MISSING_PROTOCOL
)


async def run_data_integrity_check(
    entity_id: str,
    packet: EvidencePacket,
    cycle_id: str,
    bot_id: str,
) -> tuple[str, int]:
    """Run Ray's data integrity check on the evidence packet.

    Returns:
        (data_health_summary, tokens_used)
    """
    # Build a summary of what's in the packet for Ray to inspect
    fact_summary_lines = []
    for fact in packet.structured_facts[:40]:
        fact_summary_lines.append(f"  {fact.fact_type}: {fact.value}")
    facts_text = "\n".join(fact_summary_lines) if fact_summary_lines else "No structured facts available."

    missing_text = ", ".join(packet.missing_fields) if packet.missing_fields else "None"

    user_prompt = (
        f"## Entity: {entity_id}\n\n"
        f"## Structured Facts ({len(packet.structured_facts)} total):\n"
        f"{facts_text}\n\n"
        f"## Missing Fields:\n{missing_text}\n\n"
        f"Inspect this data for quality issues. Are there anomalies, missing candles, "
        f"suspicious volume spikes, or stale prices?"
    )

    tokens_used = 0
    try:
        response, tokens, ms = await call_prism_agent(
            agent_id="CUSTOM_DATA_JANITOR_AGENT",
            user_message=user_prompt,
            fallback_system_prompt=DATA_INTEGRITY_SYSTEM_PROMPT,
            fallback_agent_name="data_integrity",
            temperature=LLM_TEMPERATURES.get("data_integrity", 0.2),
            max_tokens=1024,
            priority=Priority.NORMAL,
            ticker=entity_id,
            cycle_id=cycle_id,
            bot_id=bot_id,
        )
        tokens_used = tokens or 0
        logger.info(
            "[DATA_INTEGRITY] %s: Ray completed check in %dms (%d tokens)",
            entity_id, ms, tokens_used,
        )

        # Try to extract structured data for logging
        from app.utils.text_utils import parse_json_response
        try:
            parsed = parse_json_response(response)
            health = parsed.get("data_health", "SUSPECT")
            issues = parsed.get("issues", [])
            if issues:
                logger.warning(
                    "[DATA_INTEGRITY] %s: Ray found %d issues (health=%s): %s",
                    entity_id, len(issues), health, issues[:3],
                )
            else:
                logger.info("[DATA_INTEGRITY] %s: Data health = %s", entity_id, health)
        except Exception:
            pass

        return response.strip(), tokens_used

    except Exception as e:
        logger.error("[DATA_INTEGRITY] Ray failed for %s: %s", entity_id, e)
        return f"Data integrity check failed: {e}", 0
