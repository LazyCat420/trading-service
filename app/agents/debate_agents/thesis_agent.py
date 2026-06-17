"""
Thesis Agent — Generates a structured trading thesis from an EvidencePacket.

When an adversarial debate result is present in extra_context, this agent
acts as a synthesis writer (lower temperature, formats the judge's verdict)
rather than reasoning from scratch.

Philosophy: Baron Funds First Principles + Da Vinci evaluation framework.
Every thesis must evaluate management quality, competitive moat, and long-term
value creation potential — not just short-term price targets.
"""

import logging
from app.services.vllm_client import llm, Priority
from app.services.prism_agent_caller import call_prism_agent
from app.utils.text_utils import parse_json_response
from app.config.config_cognition import LLM_TEMPERATURES
from app.cognition.contracts.evidence import EvidencePacket
from app.cognition.contracts.debate import ThesisDraft
from app.config.investment_philosophy import (
    BARON_FIRST_PRINCIPLES, CONVICTION_FRAMEWORK, LONG_TERM_INVESTMENT_MANDATE,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """You are an expert investment analyst at a long-term quality investment firm.
Your job is to synthesize a structured investment thesis based ONLY on the provided evidence.
We are OWNERS of businesses, not traders of stocks. Every thesis must evaluate long-term value creation.

Do not invent facts. If evidence is missing, state that.
Be specific with numbers, dates, and names provided in the context.

DECISION FRAMEWORK — apply these rules strictly:
{constitution_rules}
""" + BARON_FIRST_PRINCIPLES + CONVICTION_FRAMEWORK + LONG_TERM_INVESTMENT_MANDATE + """

IMPORTANT — EXISTING POSITIONS:
If a CURRENT POSITION STATUS block is provided, evaluate whether our ORIGINAL THESIS is still intact.
- A position with unrealized losses is NOT automatically a SELL — ask "has the business deteriorated, or just the stock price?"
- A position approaching stop-loss requires evaluating: is this a permanent impairment or temporary volatility?
- HOLD is an active decision that requires articulating why the thesis remains valid.
- SELL should be recommended when: the thesis is broken, a better opportunity requires the capital, or management integrity is compromised.

GROUNDING REQUIREMENTS (critical for evaluation):
- Your "rationale" MUST quote at least 2 specific data points VERBATIM from the provided evidence
  with source labels (e.g., "RSI(14): 62.3 [technical_data]", "P/E: 22.48 [fundamental_data]").
- Do NOT cite any numeric values that are not explicitly present in the evidence below.
- If a MACRO STRATEGY MEMO is provided, your rationale MUST acknowledge the current market regime and how it influences this thesis.
- State one explicit invalidation condition (e.g., "Thesis invalidated if ROIC drops below 10%" or "Invalidated if management exits positions").
- You MUST include a management quality assessment and competitive moat evaluation in your rationale.

Respond with exactly this JSON schema:
{{
  "action": "{allowed_actions}",
  "confidence": 85,
  "conviction": "WATCH | LOW | MODERATE | HIGH | EXTREME",
  "management_quality": "1-2 sentence assessment of management team quality and alignment",
  "competitive_moat": "1-2 sentence assessment of durable competitive advantage",
  "core_claims": ["claim 1", "claim 2", "claim 3"],
  "evidence_refs": ["ref 1", "ref 2"],
  "weaknesses": ["known weakness 1", "known weakness 2"],
  "devils_advocate": "strongest argument against your recommended action",
  "invalidation_condition": "specific, measurable condition that would break this thesis",
  "rationale": "3-6 sentences: cite specific data, evaluate business quality, reference macro context, explain long-term value thesis"
}}"""


# Used when adversarial debate results are present — synthesis mode
SYNTHESIS_SYSTEM_PROMPT = """You are an investment thesis writer at a long-term quality investment firm.
We operate with a Baron Funds First Principles approach — we are owners of businesses, not traders of stocks.

An adversarial debate has ALREADY been conducted between bull and bear analysts,
with claims verified against ground truth and judged by a neutral arbiter.
The DEBATE RESULT is provided in the context below.

Your job is to FORMAT the debate verdict into a clean, structured investment thesis.
Do NOT override the judge's decision. Instead:
1. Adopt the judge's action and confidence as your starting point
2. Cite the verified claims from the winning side
3. Acknowledge the strongest counter-arguments from the losing side as weaknesses
4. Evaluate the thesis through a long-term ownership lens
5. State an explicit invalidation condition focused on business fundamentals, not short-term price

Respond with exactly this JSON schema:
{{
  "action": "{allowed_actions}",
  "confidence": 85,
  "conviction": "WATCH | LOW | MODERATE | HIGH | EXTREME",
  "management_quality": "1-2 sentence assessment of management team quality and alignment",
  "competitive_moat": "1-2 sentence assessment of durable competitive advantage",
  "core_claims": ["verified claim 1", "verified claim 2", "verified claim 3"],
  "evidence_refs": ["ref 1", "ref 2"],
  "weaknesses": ["counter-argument 1", "counter-argument 2"],
  "devils_advocate": "strongest argument against the recommended action",
  "invalidation_condition": "specific condition that would break this thesis",
  "rationale": "3-6 sentences: synthesize the debate verdict, cite verified values, evaluate long-term business quality"
}}"""


USER_TEMPLATE = """## Entity ID: {entity_id}
## Bias / Direction: {bias}

## Available Claims:
{claims_text}

## Structured Facts:
{structured_facts}

## Context / Missing Data:
{context_meta}

Construct a trading thesis with the requested bias. Make sure you acknowledge the missing data in your weaknesses.
"""


async def generate_thesis(
    entity_id: str,
    packet: EvidencePacket,
    bias: str = "neutral",
    cycle_id: str = "",
    bot_id: str = "",
    extra_context: str = "",
    watchlist: list[str] | None = None,
    held: bool = False,
) -> tuple[ThesisDraft, int]:
    """Generate a structured thesis from an EvidencePacket.

    Returns:
        (ThesisDraft, tokens_used) — the draft and total tokens consumed.
    """

    claims_text = "\n".join(
        [
            f"- [{c.provenance.source_table}] {c.subject_entity_id} {c.predicate} {c.object_value} (conf: {c.confidence:.2f})"
            for c in packet.claims[:20]
        ]
    )
    if not claims_text:
        claims_text = "No explicit claims available."

    meta = []
    if packet.contradictions:
        meta.append(
            "Contradictions: "
            + "; ".join([c.description for c in packet.contradictions])
        )
    if packet.missing_fields:
        meta.append("Missing Critical Data: " + "; ".join(packet.missing_fields))
    context_meta = (
        "\n".join(meta) if meta else "No known contradictions or missing data."
    )

    user_prompt = USER_TEMPLATE.format(
        entity_id=entity_id,
        bias=bias,
        claims_text=claims_text,
        structured_facts=str(packet.structured_facts or {}),
        context_meta=context_meta,
    )

    # Inject ontology/macro context if provided
    if extra_context:
        user_prompt = extra_context.strip() + "\n\n" + user_prompt

    # Fix: Reinforce JSON output requirement at the END of the user prompt.
    # This is the last thing the LLM sees before generating, making it more
    # likely to comply with the JSON schema even when the Prism persona
    # encourages writing markdown analysis reports.
    user_prompt += (
        "\n\n---\nCRITICAL OUTPUT FORMAT REQUIREMENT: You MUST respond with ONLY a valid JSON object "
        "matching the schema described in your system prompt. Do NOT write a markdown report, "
        "do NOT use headers or tables, do NOT wrap in code fences. Output ONLY the raw JSON object."
    )

    # Inject watchlist peer context so the LLM knows what other tickers
    # are being analysed in the same cycle.
    if watchlist:
        peers = [t for t in watchlist if t.upper() != entity_id.upper()]
        if peers:
            user_prompt += f"\n\n## Peer Tickers in This Cycle:\n{', '.join(peers)}"

    tokens_used = 0

    # Determine mode: synthesis (debate present) or independent reasoning
    is_synthesis = extra_context and "ADVERSARIAL DEBATE RESULT" in extra_context
    from app.db.constitution import format_constitution_for_prompt
    from app.cognition.debate.action_gate import get_allowed_actions_str, gate_action

    allowed_actions = get_allowed_actions_str(held)

    active_prompt = (
        SYNTHESIS_SYSTEM_PROMPT.format(allowed_actions=allowed_actions)
        if is_synthesis
        else SYSTEM_PROMPT_TEMPLATE.format(
            constitution_rules=format_constitution_for_prompt(),
            allowed_actions=allowed_actions,
        )
    )
    active_temp_key = "thesis_synthesis" if is_synthesis else "thesis_generation"
    if is_synthesis:
        logger.info(
            "[THESIS] Synthesis mode — formatting debate verdict for %s", entity_id
        )

    try:
        response, tokens, ms = await call_prism_agent(
            agent_id="CUSTOM_THESIS_AGENT",
            user_message=user_prompt,
            fallback_system_prompt=active_prompt,
            fallback_agent_name="thesis_agent",
            temperature=LLM_TEMPERATURES.get(active_temp_key, 0.5),
            max_tokens=2048,
            priority=Priority.NORMAL,
            ticker=entity_id,
            cycle_id=cycle_id,
            bot_id=bot_id,
        )
        tokens_used = tokens or 0
        data = parse_json_response(response)
        if not data or "action" not in data:
            # JSON parse failed completely — the LLM likely returned markdown.
            # Log the warning but don't treat as a hard failure yet;
            # parse_json_response already tried parse_malformed_text_response.
            logger.warning(
                "[THESIS] parse_json_response returned empty/invalid dict for %s. "
                "Data: %r. Raw response preview: %.300r",
                entity_id, data, response[:300] if response else "",
            )
            # Persist raw LLM output for post-mortem debugging
            try:
                from app.log_manager import log_manager
                log_manager.log_cycle_error(
                    cycle_id, "thesis_json_parse_failure",
                    ticker=entity_id, error="parse_json_response returned empty/invalid dict",
                    stage="thesis_generation",
                    extra={"raw_llm_response": (response[:2000] if response else "")},
                )
            except Exception:
                pass
        elif int(data.get("confidence", 0)) == 0 and not data.get("core_claims"):
            # Got an action but degenerate signal — log as info, not warning.
            # This may still be a valid HOLD from a markdown report extraction.
            logger.info(
                "[THESIS] Parsed response has action=%s but confidence=0 and no claims for %s. "
                "May be a markdown-extracted fallback.",
                data.get('action'), entity_id,
            )
    except Exception as e:
        logger.error("[THESIS] Failed to generate thesis: %s", e)
        # Attempt to salvage the response if it was just a parsing error
        raw_text = (
            response if "response" in locals() and isinstance(response, str) else str(e)
        )
        data = {
            "error": f"Failed to parse thesis. Reason: {e}. Raw: {raw_text[:250]}..."
        }
        # Persist raw LLM output for post-mortem debugging
        try:
            from app.log_manager import log_manager
            log_manager.log_cycle_error(
                cycle_id, "thesis_generation_exception",
                ticker=entity_id, error=str(e),
                stage="thesis_generation",
                extra={"raw_llm_response": (raw_text[:2000] if raw_text else "")},
            )
        except Exception:
            pass

    # If parsing returned an empty dict but no exception was thrown
    if not data and "response" in locals() and isinstance(response, str):
        data = {
            "error": f"Failed to parse thesis. Invalid JSON format. Raw: {response[:250]}..."
        }

    from app.utils.text_utils import coerce_str, coerce_int, coerce_list_str

    action = gate_action(coerce_str(data.get("action", "HOLD")), held)

    draft = ThesisDraft(
        action=action,
        confidence=coerce_int(data.get("confidence", 0)),
        core_claims=coerce_list_str(data.get("core_claims", [])),
        evidence_refs=coerce_list_str(data.get("evidence_refs", [])),
        weaknesses=coerce_list_str(data.get("weaknesses", [])),
        rationale=coerce_str(data.get("rationale", data.get("error", "Failed to parse thesis"))),
        iteration=0,
        conviction=coerce_str(data.get("conviction", "")),
        management_quality=coerce_str(data.get("management_quality", "")),
        competitive_moat=coerce_str(data.get("competitive_moat", "")),
        devils_advocate=coerce_str(data.get("devils_advocate", "")),
        invalidation_condition=coerce_str(data.get("invalidation_condition", "")),
    )
    return draft, tokens_used
