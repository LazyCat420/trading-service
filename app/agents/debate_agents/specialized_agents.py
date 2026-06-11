import logging
from app.services.vllm_client import llm, Priority
from app.services.prism_agent_caller import call_prism_agent
from app.config.config_cognition import LLM_TEMPERATURES
from app.cognition.contracts.evidence import EvidencePacket
from app.config.personas import get_persona_prompt
from app.config.guardrails import (
    ANTI_HALLUCINATION_BLOCK, PEER_ACCOUNTABILITY_BLOCK, DATA_MISSING_PROTOCOL,
    DEPTH_OF_ANALYSIS_BLOCK, DEVIL_ADVOCATE_BLOCK,
)
from app.config.investment_philosophy import LONG_TERM_INVESTMENT_MANDATE

logger = logging.getLogger(__name__)

CONCLUSION_RULES_BLOCK = """

[INTERACTION & CONCLUSION RULES]
1. You are on a strict time budget. Conclude within 2 paragraphs MAX.
   CRITICAL: Do NOT waste words on filler. No "Hey team, here's what I found" or "As we all know".
   Get straight to the data: "RSI is 37.8, stock is oversold." "P/E at 22x vs sector 18x — overvalued."
   Every sentence must contain a data point, a conclusion, or a challenge. Zero fluff.
2. You may tag agents to challenge or coordinate. Use these formats:
   - @Agent: [CHALLENGE] Your specific challenge to their claim
   - @Agent: [REQUEST_DATA] What specific data you need from them
   - @Agent: [DEFEND] Defending your position with data
   - DELEGATION: @Agent - a follow-up question or request
3. You MUST end your output with a definitive stance using this exact format:
   - STANCE: [BULLISH / BEARISH / NEUTRAL]
   - CONFIDENCE: [1-100]
   - CONVICTION: [WATCH / LOW / MODERATE / HIGH / EXTREME]
   - DEVIL_ADVOCATE: [Strongest argument against your stance, in one sentence]
   - DELEGATION: [Tag another agent or "NONE"]
""" + LONG_TERM_INVESTMENT_MANDATE + ANTI_HALLUCINATION_BLOCK + PEER_ACCOUNTABILITY_BLOCK + DATA_MISSING_PROTOCOL + DEPTH_OF_ANALYSIS_BLOCK + DEVIL_ADVOCATE_BLOCK


async def _run_specialized_agent(
    agent_name: str,
    system_prompt: str,
    entity_id: str,
    packet: EvidencePacket,
    cycle_id: str,
    bot_id: str,
    research_focus: str = "",
    team_findings: str = "",
) -> tuple[str, int]:
    """Helper to run a specialized agent."""
    user_prompt = f"## Entity ID: {entity_id}\n\n"
    if research_focus:
        user_prompt += f"## SPECIFIC RESEARCH DIRECTION:\n{research_focus}\n\n"
    if team_findings:
        user_prompt += f"## TEAM FINDINGS FROM OTHER AGENTS:\n{team_findings}\n\n"
    user_prompt += f"## Structured Facts:\n{packet.structured_facts}\n\nAnalyze the data from your unique perspective."

    tokens_used = 0
    try:
        response, tokens, ms = await call_prism_agent(
            agent_id=f"CUSTOM_{agent_name.upper()}",
            user_message=user_prompt,
            fallback_system_prompt=system_prompt,
            fallback_agent_name=agent_name,
            temperature=LLM_TEMPERATURES.get(agent_name, 0.3),
            max_tokens=2048,
            priority=Priority.NORMAL,
            ticker=entity_id,
            cycle_id=cycle_id,
            bot_id=bot_id,
        )
        tokens_used = tokens or 0
        return response.strip(), tokens_used
    except Exception as e:
        logger.error(f"[{agent_name.upper()}] Failed: {e}")
        return f"Failed: {e}", 0


async def analyze_sentiment(
    entity_id: str, packet: EvidencePacket, cycle_id: str, bot_id: str, research_focus: str = "", team_findings: str = ""
) -> tuple[str, int]:
    p_prompt = get_persona_prompt("BEHAVIORAL")
    sys = (
        f"{p_prompt}\n\n"
        "Analyze the social and news sentiment purely based on the provided facts.\n"
        "You MUST categorize the sentiment into one of these exact labels:\n"
        "- Strongly Bullish\n"
        "- Mildly Bullish\n"
        "- Neutral\n"
        "- Mildly Bearish\n"
        "- Strongly Bearish\n\n"
        "First, output exactly this JSON format:\n"
        "{\n"
        '  "classification": "YOUR_LABEL",\n'
        '  "rationale": "A concise explanation (max 2 sentences) based on the facts."\n'
        "}\n\n"
        "Then, immediately append the conclusion stance as text below the JSON block."
        f"{CONCLUSION_RULES_BLOCK}"
    )
    raw_response, tokens = await _run_specialized_agent(
        "sentiment_agent", sys, entity_id, packet, cycle_id, bot_id, research_focus, team_findings
    )

    from app.utils.text_utils import parse_json_response
    classification = "Neutral"
    rationale = raw_response
    try:
        data = parse_json_response(raw_response)
        if isinstance(data, dict):
            classification = data.get("classification", "Neutral").strip()
            rationale = data.get("rationale", "").strip()
    except Exception:
        # Fallback to search in raw string
        for label in ["Strongly Bullish", "Mildly Bullish", "Neutral", "Mildly Bearish", "Strongly Bearish"]:
            if label.lower() in raw_response.lower():
                classification = label
                break

    # Normalize classification label
    normalized_label = "Neutral"
    for label in ["Strongly Bullish", "Mildly Bullish", "Neutral", "Mildly Bearish", "Strongly Bearish"]:
        if classification.lower() == label.lower() or label.lower() in classification.lower():
            normalized_label = label
            break

    label_mapping = {
        "Strongly Bullish": {"score": 1.0, "hsl": "HSL(120, 100%, 40%)"},
        "Mildly Bullish": {"score": 0.5, "hsl": "HSL(90, 100%, 40%)"},
        "Neutral": {"score": 0.0, "hsl": "HSL(60, 100%, 40%)"},
        "Mildly Bearish": {"score": -0.5, "hsl": "HSL(30, 100%, 40%)"},
        "Strongly Bearish": {"score": -1.0, "hsl": "HSL(0, 100%, 40%)"},
    }

    meta = label_mapping.get(normalized_label, {"score": 0.0, "hsl": "HSL(60, 100%, 40%)"})
    formatted_result = (
        f"Classification: {normalized_label}\n"
        f"Score: {meta['score']}\n"
        f"Color: {meta['hsl']}\n"
        f"Rationale: {rationale}\n\n"
        f"{raw_response}"
    )
    return formatted_result, tokens


async def analyze_macro_risk(
    entity_id: str, packet: EvidencePacket, cycle_id: str, bot_id: str, research_focus: str = "", team_findings: str = ""
) -> tuple[str, int]:
    sys = get_persona_prompt("RISK") + CONCLUSION_RULES_BLOCK
    return await _run_specialized_agent(
        "macro_risk_agent", sys, entity_id, packet, cycle_id, bot_id, research_focus, team_findings
    )


async def analyze_fundamentals(
    entity_id: str, packet: EvidencePacket, cycle_id: str, bot_id: str, research_focus: str = "", team_findings: str = ""
) -> tuple[str, int]:
    sys = get_persona_prompt("FUNDAMENTAL") + CONCLUSION_RULES_BLOCK
    return await _run_specialized_agent(
        "fundamental_agent", sys, entity_id, packet, cycle_id, bot_id, research_focus, team_findings
    )

async def analyze_quantitative_critique(
    entity_id: str, packet: EvidencePacket, cycle_id: str, bot_id: str, research_focus: str = "", team_findings: str = ""
) -> tuple[str, int]:
    """Dr. Aris's quantitative critique — reads Priya/Vance's findings and critiques them mathematically."""
    p_prompt = get_persona_prompt("QUANT")
    sys = (
        f"{p_prompt}\n\n"
        "You have just read the Fundamental Analyst's (Priya) and Sentiment Trader's (Vance) findings "
        "from the TaskBoard. Your job is to CRITIQUE their analysis mathematically.\n\n"
        "SPECIFICALLY:\n"
        "- If Priya argues the fundamentals are strong, check if the moving averages, RSI, "
        "and volatility (ATR/Bollinger) support or contradict her thesis.\n"
        "- If Vance argues sentiment is bullish/bearish, check if the price action and volume "
        "patterns confirm or deny the crowd's mood.\n"
        "- Point out any statistical anomalies: variance outside 3σ, broken support/resistance, "
        "divergences between price and indicators.\n"
        "- Be direct. If the math says the fundamental thesis is wrong, say so.\n\n"
        "Example: \"Priya, your fundamental thesis is cute, but the 200-day moving average is broken "
        "and variance is outside 3 standard deviations. It's a bad trade.\"\n\n"
        "Output exactly this JSON:\n"
        "{\n"
        '  "critique_of_fundamental": "your mathematical critique of Priya\'s thesis",\n'
        '  "critique_of_sentiment": "your mathematical critique of Vance\'s thesis",\n'
        '  "mathematical_assessment": "1-2 sentence quantitative summary",\n'
        '  "key_indicators": {"rsi": "value", "atr": "value", "sma_200": "value"}\n'
        "}\n"
        + CONCLUSION_RULES_BLOCK
    )
    return await _run_specialized_agent(
        "quant_critique_agent", sys, entity_id, packet, cycle_id, bot_id, research_focus, team_findings
    )


async def analyze_deep_research(
    entity_id: str, packet: EvidencePacket, cycle_id: str, bot_id: str, research_focus: str = "", team_findings: str = ""
) -> tuple[str, int]:
    sys = ("You are a Deep Research Agent. The provided data is highly redundant. Your mission is to find unique, non-obvious catalysts and hidden risks that the consensus is missing."
           + ANTI_HALLUCINATION_BLOCK + DATA_MISSING_PROTOCOL + CONCLUSION_RULES_BLOCK)
    return await _run_specialized_agent(
        "deep_research_agent", sys, entity_id, packet, cycle_id, bot_id, research_focus, team_findings
    )


