"""
Family Office V3 — Manager Agents.

Defines the 8 persistent Manager agent system prompts and execution
functions for the Baron Funds Family Office architecture.

Each Manager:
  - Receives evidence filtered to its domain
  - Applies role-specific reasoning (first-principles, analogical, etc.)
  - Submits a ManagerArgument with structured claims
  - Can request additional data via DataRequest

All LLM calls go through app.services.vllm_client (Rule 2).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.cognition.contracts.family_office import (
    CIODirective,
    CIODirectiveStatus,
    DataRequest,
    FamilyOfficeVerdict,
    ManagerArgument,
    ManagerRole,
    WorkerType,
)
from app.config.investment_philosophy import (
    BARON_FIRST_PRINCIPLES,
    CONVICTION_FRAMEWORK,
    LONG_TERM_INVESTMENT_MANDATE,
)
from app.services.vllm_client import llm, Priority
from app.utils.text_utils import parse_json_response

logger = logging.getLogger(__name__)


# ── Manager System Prompts ──────────────────────────────────────────────

MANAGER_PROMPTS: dict[ManagerRole, str] = {
    ManagerRole.FUNDAMENTAL_PM: f"""You are Priya, the Fundamental Value PM at an elite long-term investment Family Office.

YOUR ROLE: Deconstruct business problems to core truths. Focus on long-term business potential, cash flow generation, management quality, and competitive moats.

REASONING APPROACH: First Principles
- Break down the business to its fundamental components
- Evaluate intrinsic value from bottom-up: revenue drivers, margin structure, capital allocation
- Assess management quality and their track record of execution
- Identify durable competitive advantages (moats)

{BARON_FIRST_PRINCIPLES}
{CONVICTION_FRAMEWORK}

CRITICAL RULES:
- Every claim MUST end with an inline citation: [source:value]
- Do NOT invent data. Only cite values from the provided evidence.
- If you need data not in the evidence, submit a data_request — do NOT hallucinate.
- Focus on business quality, not short-term price movements.

OUTPUT FORMAT (JSON):
{{
  "claims": ["claim 1 with [source:value]", "claim 2 with [source:value]"],
  "confidence": 0-100,
  "conviction": "WATCH|LOW|MODERATE|HIGH|EXTREME",
  "key_argument": "single strongest argument",
  "devils_advocate": "strongest argument AGAINST your case",
  "data_requests": [
    {{"worker_type": "worker_fundamental", "description": "what data you need", "priority": "critical|normal|optional", "specific_metrics": ["metric1", "metric2"]}}
  ]
}}""",

    ManagerRole.GROWTH_PM: f"""You are Dr. Aris, the Growth & Momentum PM at an elite long-term investment Family Office.

YOUR ROLE: Analyze market trends, technical indicators, and price momentum. Utilize analogical reasoning to compare current setups to historical cycles.

REASONING APPROACH: Analogical
- Compare current technical setup to historical patterns from the Brain Graph
- Identify momentum shifts, trend breaks, and cycle positioning
- Evaluate volume patterns, relative strength, and moving average convergence
- Cross-reference current setup with prior cycles: "This looks like X setup from Y period"

{CONVICTION_FRAMEWORK}

CRITICAL RULES:
- Every claim MUST end with an inline citation: [source:value]
- Do NOT invent data. Only cite values from the provided evidence.
- If you need data not in the evidence, submit a data_request — do NOT hallucinate.
- Focus on trend and momentum signals, not narratives.

OUTPUT FORMAT (JSON):
{{
  "claims": ["claim 1 with [source:value]", "claim 2 with [source:value]"],
  "confidence": 0-100,
  "conviction": "WATCH|LOW|MODERATE|HIGH|EXTREME",
  "key_argument": "single strongest argument",
  "devils_advocate": "strongest argument AGAINST your case",
  "data_requests": [
    {{"worker_type": "worker_quant", "description": "what data you need", "priority": "critical|normal|optional", "specific_metrics": ["metric1"]}}
  ]
}}""",

    ManagerRole.MACRO_PM: f"""You are Vance, the Macro & Sentiment PM at an elite long-term investment Family Office.

YOUR ROLE: Analyze sector flows, consumer narratives, macroeconomic shifts, and sentiment signals. You are a contrarian — if the crowd is euphoric, you suspect a trap.

REASONING APPROACH: Inductive
- Collect specific data points (news sentiment, sector flows, social signals) and build generalizations
- Track narrative shifts: what story is the market telling, and is it changing?
- Evaluate institutional vs retail positioning
- Identify sentiment extremes (euphoria or capitulation) as contrarian signals

{CONVICTION_FRAMEWORK}

CRITICAL RULES:
- Every claim MUST end with an inline citation: [source:value]
- Do NOT invent data. Only cite values from the provided evidence.
- If you need data not in the evidence, submit a data_request — do NOT hallucinate.
- Weigh evidence by source credibility: SEC filings > official news > Reddit posts.

OUTPUT FORMAT (JSON):
{{
  "claims": ["claim 1 with [source:value]", "claim 2 with [source:value]"],
  "confidence": 0-100,
  "conviction": "WATCH|LOW|MODERATE|HIGH|EXTREME",
  "key_argument": "single strongest argument",
  "devils_advocate": "strongest argument AGAINST your case",
  "data_requests": [
    {{"worker_type": "worker_news", "description": "what data you need", "priority": "critical|normal|optional", "specific_metrics": ["metric1"]}}
  ]
}}""",

    ManagerRole.RISK_MANAGER: f"""You are Helen, the Risk Manager (Devil's Advocate) at an elite long-term investment Family Office.

YOUR ROLE: Continuously question assumptions. You are solely focused on identifying the probability of an adverse outcome leading to the PERMANENT LOSS OF CAPITAL. You are the guardian against catastrophic risk.

REASONING APPROACH: Adversarial / Pre-Mortem
- Assume the worst-case scenario and work backward: what would cause permanent capital loss?
- Challenge every bull thesis: what if revenue misses? What if the moat erodes?
- Evaluate position sizing, concentration risk, and correlation to existing portfolio
- Identify binary event risks (earnings, FDA, litigation) that could gap the stock
- Calculate risk-reward ratios and stop-loss levels

{CONVICTION_FRAMEWORK}

CRITICAL RULES:
- Every claim MUST end with an inline citation: [source:value]
- Do NOT invent data. Only cite values from the provided evidence.
- If you need data not in the evidence, submit a data_request — do NOT hallucinate.
- Your job is to PROTECT capital, not to find reasons to buy.
- If you cannot find significant risks, say so — but always look hard.

OUTPUT FORMAT (JSON):
{{
  "claims": ["risk 1 with [source:value]", "risk 2 with [source:value]"],
  "confidence": 0-100,
  "conviction": "WATCH|LOW|MODERATE|HIGH|EXTREME",
  "key_argument": "the biggest risk to this position",
  "devils_advocate": "strongest argument that the risk is manageable",
  "data_requests": [
    {{"worker_type": "worker_quant", "description": "what risk data you need", "priority": "critical|normal|optional", "specific_metrics": ["metric1"]}}
  ]
}}""",

    ManagerRole.MEMORY_PM: """You are Mnemosyne, the Memory & Context PM at an elite long-term investment Family Office.

YOUR ROLE: Query the Brain Graph and historical memory to inject lessons learned from previous cycles into the current debate. You ensure the team doesn't repeat past mistakes.

REASONING APPROACH: Historical Analogical
- Search for prior analyses of this ticker or similar setups
- Surface past trade outcomes: what worked, what failed, and why
- Identify patterns: "Last time we saw this RSI + sentiment combo, the result was..."
- Provide procedural memory: trading rules and constitution amendments relevant to this ticker

CRITICAL RULES:
- Every claim MUST end with an inline citation: [memory:source]
- Only cite actual historical data from memory — do NOT invent past events.
- If no relevant memory exists, say so clearly.
- Focus on ACTIONABLE lessons, not general platitudes.

OUTPUT FORMAT (JSON):
{
  "claims": ["lesson 1 with [memory:source]", "lesson 2 with [memory:source]"],
  "confidence": 0-100,
  "conviction": "WATCH|LOW|MODERATE|HIGH|EXTREME",
  "key_argument": "most relevant historical lesson",
  "devils_advocate": "why the historical analogy might not apply here",
  "data_requests": []
}""",
}

# Cross-Examiner uses the existing prompt from agents/custom/debate_cross_examiner.py
# CIO prompt is built dynamically in run_cio_evaluation()
# Worker Orchestrator doesn't need a system prompt — it dispatches based on DataRequests


# ── Evidence Filters (which data each PM sees) ──────────────────────────

MANAGER_EVIDENCE_FILTER: dict[ManagerRole, list[str]] = {
    ManagerRole.FUNDAMENTAL_PM: [
        "pe_ratio", "earnings", "revenue", "margins", "debt", "fcf",
        "book_value", "dividend", "eps", "roe", "roa", "p_e", "p_b",
        "operating", "net_income", "balance", "cash_flow", "valuation",
        "fundamental", "financial", "ratio",
    ],
    ManagerRole.GROWTH_PM: [
        "rsi", "sma", "ema", "volume", "macd", "bollinger", "atr",
        "moving_average", "momentum", "price", "close", "open", "high",
        "low", "support", "resistance", "trend", "technical", "indicator",
    ],
    ManagerRole.MACRO_PM: [
        "fed_rate", "sector_flow", "news_sentiment", "reddit_score",
        "interest_rate", "inflation", "gdp", "unemployment", "sentiment",
        "macro", "catalyst", "industry", "social", "news", "youtube",
        "congress", "institutional", "insider",
    ],
    ManagerRole.RISK_MANAGER: [
        # Risk sees everything — needs full picture for risk assessment
    ],
    ManagerRole.MEMORY_PM: [
        # Memory PM sees everything — needs full context for analogies
    ],
}


# ── Manager Temperatures ────────────────────────────────────────────────

MANAGER_TEMPERATURES: dict[ManagerRole, float] = {
    ManagerRole.FUNDAMENTAL_PM: 0.3,    # Precise with numbers
    ManagerRole.GROWTH_PM: 0.5,          # Pattern interpretation varies
    ManagerRole.MACRO_PM: 0.7,           # Narrative/sentiment is fuzzy
    ManagerRole.RISK_MANAGER: 0.3,       # Risk must be precise
    ManagerRole.MEMORY_PM: 0.4,          # Historical recall should be accurate
    ManagerRole.CROSS_EXAMINER: 0.2,     # Forensic — low creativity
}


def filter_packet_for_manager(
    packet: "EvidencePacket",
    role: ManagerRole,
) -> "EvidencePacket":
    """Return a filtered evidence packet for this manager's focus area.

    Managers with empty filter lists (Risk, Memory) see the full packet.
    """
    allowed_keys = MANAGER_EVIDENCE_FILTER.get(role)
    if not allowed_keys:
        return packet  # Full access for Risk, Memory, etc.

    filtered_facts = [
        f for f in packet.structured_facts
        if any(k in f.fact_type.lower() for k in allowed_keys)
    ]

    # Fall back to full packet if filtering removed everything
    if not filtered_facts and packet.structured_facts:
        logger.warning(
            "[V3] Evidence filter for %s matched 0/%d facts — using full packet",
            role.value, len(packet.structured_facts),
        )
        return packet

    return packet.model_copy(update={"structured_facts": filtered_facts})


def _build_evidence_text(packet: "EvidencePacket") -> str:
    """Build a text representation of the evidence packet for prompts."""
    lines = ["## EVIDENCE FILE (cite directly):"]
    for f in packet.structured_facts:
        lines.append(f"  {f.fact_type}: {f.value}")
    if getattr(packet, "tool_cache", None):
        lines.append("## PRE-FETCHED TOOL DATA:")
        for tool_name, result in packet.tool_cache.items():
            lines.append(f"  [{tool_name}]: {result[:500]}")
    return "\n".join(lines)


def _build_source_context(packet: "EvidencePacket") -> str:
    """Build unstructured context (news, Reddit, YouTube) text."""
    if not packet.source_summaries:
        return "None available."

    from app.cognition.debate.debate_coordinator import format_source_ref_for_prompt
    return "\n".join(
        format_source_ref_for_prompt(s) for s in packet.source_summaries[:15]
    )


async def run_manager_analysis(
    role: ManagerRole,
    ticker: str,
    packet: "EvidencePacket",
    cycle_id: str,
    bot_id: str,
    position_context: dict | None = None,
    portfolio_dashboard: str = "",
    memory_context: str = "",
    prior_round_summary: str = "",
    worker_results_text: str = "",
) -> ManagerArgument:
    """Run a single Manager PM's analysis and return their argument.

    The PM receives filtered evidence, applies its reasoning approach,
    and submits a structured ManagerArgument. If it needs more data,
    it includes DataRequests in its response.
    """
    from app.cognition.contracts.evidence import EvidencePacket
    from app.services.prism_agent_caller import call_prism_agent

    system_prompt = MANAGER_PROMPTS.get(role)
    if not system_prompt:
        logger.warning("[V3] No system prompt for manager %s — skipping", role.value)
        return ManagerArgument(role=role)

    # Filter evidence to this manager's domain
    filtered_packet = filter_packet_for_manager(packet, role)

    # Build user prompt
    evidence_text = _build_evidence_text(filtered_packet)
    source_context = _build_source_context(packet)

    # Position context for held positions
    position_block = ""
    if position_context and position_context.get("held"):
        try:
            from app.tools.portfolio_tools import format_position_context_for_prompt
            position_block = format_position_context_for_prompt(position_context)
        except Exception:
            pass

    user_prompt = f"""## Entity: {ticker}

{position_block}

{portfolio_dashboard}

{evidence_text}

## Unstructured Context (News/Reddit/YouTube):
{source_context}

"""

    # Inject memory context if available
    if memory_context:
        user_prompt += f"""## HISTORICAL MEMORY (from Brain Graph):
{memory_context}

"""

    # Inject prior round context if this is a re-analysis
    if prior_round_summary:
        user_prompt += f"""## PRIOR ROUND CONTEXT (CIO requested more data):
{prior_round_summary}

"""

    # Inject new worker data if available
    if worker_results_text:
        user_prompt += f"""## NEW DATA (fetched by Worker Analysts this round):
{worker_results_text}

"""

    user_prompt += "Analyze the evidence above and submit your argument as JSON."

    # Budget-aware truncation
    from app.config.context_budget import get_context_budget
    budget = get_context_budget()
    if len(user_prompt) > budget.data_context_chars:
        user_prompt = user_prompt[:budget.data_context_chars]
        user_prompt += "\n... [truncated for context budget]"

    temperature = MANAGER_TEMPERATURES.get(role, 0.4)

    try:
        response, tokens, ms = await call_prism_agent(
            agent_id=f"CUSTOM_V3_{role.value.upper()}",
            user_message=user_prompt,
            fallback_system_prompt=system_prompt,
            fallback_agent_name=f"v3_{role.value}",
            temperature=temperature,
            max_tokens=4096,
            priority=Priority.NORMAL,
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
        )

        parsed = parse_json_response(response)
        logger.info(
            "[V3] Manager %s for %s: %d tokens, %dms, %d claims",
            role.value, ticker, tokens or 0, ms,
            len(parsed.get("claims", [])),
        )

        # Parse data requests from response
        data_requests = []
        for dr in parsed.get("data_requests", []):
            if isinstance(dr, dict) and dr.get("description"):
                try:
                    worker_type_str = dr.get("worker_type", "worker_fundamental")
                    # Normalize worker type string
                    try:
                        wt = WorkerType(worker_type_str)
                    except ValueError:
                        wt = WorkerType.FUNDAMENTAL
                    data_requests.append(DataRequest(
                        requesting_manager=role,
                        worker_type=wt,
                        description=dr["description"],
                        priority=dr.get("priority", "normal"),
                        ticker=ticker,
                        specific_metrics=dr.get("specific_metrics", []),
                    ))
                except Exception as dr_err:
                    logger.debug("[V3] Failed to parse data request from %s: %s", role.value, dr_err)

        return ManagerArgument(
            role=role,
            claims=parsed.get("claims", []),
            confidence=int(parsed.get("confidence", 0)),
            conviction=parsed.get("conviction", ""),
            key_argument=parsed.get("key_argument", ""),
            devils_advocate=parsed.get("devils_advocate", ""),
            data_requests=data_requests,
            reasoning_approach=role.value,
            raw_response=response,
            tokens_used=tokens or 0,
        )

    except Exception as e:
        logger.error("[V3] Manager %s failed for %s: %s", role.value, ticker, e)
        return ManagerArgument(role=role, raw_response=str(e))


async def run_cross_examination(
    pm_arguments: list[ManagerArgument],
    ticker: str,
    packet: "EvidencePacket",
    cycle_id: str,
    bot_id: str,
) -> str:
    """Run the Cross-Examiner to verify all PM claims against evidence.

    Returns a string summary of findings (verified/unverified claims).
    """
    from app.agents.custom.debate_cross_examiner import IDENTITY as CROSS_EXAM_SYSTEM_PROMPT
    from app.services.prism_agent_caller import call_prism_agent
    import json

    # Collect all claims by manager
    claims_by_manager = {}
    for arg in pm_arguments:
        if arg.claims:
            claims_by_manager[arg.role.value] = arg.claims

    if not claims_by_manager:
        return "No claims to cross-examine."

    facts_text = str(packet.structured_facts or {})[:10000]
    source_context = _build_source_context(packet)

    user_prompt = f"""## ALL MANAGER CLAIMS TO VERIFY:
{json.dumps(claims_by_manager, indent=2)}

## ACTUAL STRUCTURED FACTS (ground truth):
{facts_text}

## UNSTRUCTURED CONTEXT:
{source_context[:5000]}

Cross-examine ALL claims against the actual data.
NOTE: Be highly tolerant of minor decimal rounding differences and shorthand notations.
For each claim, mark it as VERIFIED or UNVERIFIED with a brief explanation."""

    try:
        response, tokens, ms = await call_prism_agent(
            agent_id="CUSTOM_V3_CROSS_EXAMINER",
            user_message=user_prompt,
            fallback_system_prompt=CROSS_EXAM_SYSTEM_PROMPT,
            fallback_agent_name="v3_cross_examiner",
            temperature=0.2,
            max_tokens=4096,
            priority=Priority.NORMAL,
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
        )
        logger.info("[V3] Cross-examiner for %s: %d tokens, %dms", ticker, tokens or 0, ms)
        return response
    except Exception as e:
        logger.error("[V3] Cross-examiner failed for %s: %s", ticker, e)
        return f"Cross-examination failed: {e}"


# ── CIO (Chief Investment Officer) ──────────────────────────────────────

CIO_SYSTEM_PROMPT = f"""You are The Boss, the Chief Investment Officer (CIO) of an elite long-term investment Family Office.

Your specialists have posted their analyses on the TaskBoard. Your job is to make an EXECUTIVE DECISION.

{BARON_FIRST_PRINCIPLES}
{LONG_TERM_INVESTMENT_MANDATE}
{CONVICTION_FRAMEWORK}

## YOUR DECISION PROCESS:
1. Review each PM's argument, confidence, and conviction level
2. Review the Cross-Examiner's findings — discount claims that were UNVERIFIED
3. Weigh the Risk Manager's concerns seriously — permanent capital loss is unacceptable
4. Check Memory PM's historical context — have we seen this pattern before?
5. DECIDE: Do you have enough evidence to render a verdict, or do you need more data?

## TWO POSSIBLE OUTPUTS:

### If you NEED MORE DATA:
{{
  "status": "needs_more_data",
  "rationale": "Why the evidence is insufficient",
  "data_requests": [
    {{"worker_type": "worker_quant|worker_fundamental|worker_news|worker_insider", "description": "what specific data you need", "priority": "critical", "specific_metrics": ["metric"]}}
  ],
  "directed_managers": ["fundamental_pm", "growth_pm"]
}}

### If you are READY FOR VERDICT:
{{
  "status": "ready_for_verdict",
  "action": "BUY|SELL|HOLD",
  "confidence": 0-100,
  "winning_side": "bull|bear|split",
  "conviction": "WATCH|LOW|MODERATE|HIGH|EXTREME",
  "key_deciding_factor": "the specific claim that tipped the balance",
  "rejected_claim_impact": "how unverified claims affected your confidence",
  "rationale": "2-4 sentences citing specific verified values and explaining which PM convinced you",
  "original_thesis_status": "VALID|PARTIALLY_VALID|INVALIDATED|NOT_HELD",
  "original_thesis_explanation": "explanation of thesis status"
}}

RULES:
- You may NOT introduce new data points not cited by any PM.
- Claims that were UNVERIFIED should be discounted.
- The Risk Manager's concerns about permanent capital loss carry EXTRA weight.
- If PMs need more data, say so — don't force a verdict on thin evidence.
- But if you've already looped {{round_number}} times, make the best decision you can.
{{hold_rule}}"""


async def run_cio_evaluation(
    pm_arguments: list[ManagerArgument],
    cross_exam_findings: str,
    ticker: str,
    cycle_id: str,
    bot_id: str,
    round_number: int,
    max_rounds: int,
    held: bool = False,
    position_context: dict | None = None,
) -> CIODirective | FamilyOfficeVerdict:
    """Run the CIO's evaluation of all PM arguments.

    Returns either a CIODirective (needs more data / abstain) or
    a FamilyOfficeVerdict (final decision).
    """
    from app.services.prism_agent_caller import call_prism_agent
    from app.cognition.debate.action_gate import gate_action, get_allowed_actions_str

    # Build position-aware system prompt
    if held:
        hold_rule = ""
        allowed = get_allowed_actions_str(held)
    else:
        hold_rule = (
            "\n- You MUST NOT output HOLD. The bot does not own this stock. "
            "You must decide BUY or SELL based on the evidence.\n"
        )
        allowed = get_allowed_actions_str(held)

    is_final_round = round_number >= max_rounds
    system_prompt = CIO_SYSTEM_PROMPT.format(
        round_number=round_number,
        hold_rule=hold_rule,
    )

    if is_final_round:
        system_prompt += (
            f"\n\nCRITICAL: This is round {round_number}/{max_rounds}. "
            "You MUST render a final verdict NOW. No more data requests allowed. "
            "Make the best decision you can with the evidence available."
        )

    # Build user prompt with all PM arguments
    pm_sections = []
    for arg in pm_arguments:
        section = f"### {arg.role.value.upper()} (confidence: {arg.confidence}%, conviction: {arg.conviction})\n"
        section += f"Key argument: {arg.key_argument}\n"
        section += f"Devil's advocate: {arg.devils_advocate}\n"
        section += "Claims:\n"
        for c in arg.claims:
            survived = " [SURVIVED REBUTTAL]" if round_number > 1 else ""
            section += f"  - {c}{survived}\n"
        pm_sections.append(section)

    position_block = ""
    if held and position_context:
        try:
            from app.tools.portfolio_tools import format_position_context_for_prompt
            position_block = format_position_context_for_prompt(position_context)
        except Exception:
            pass

    user_prompt = f"""## Ticker: {ticker}
## Round: {round_number}/{max_rounds}

{position_block}

## PM ARGUMENTS:
{"".join(pm_sections)}

## CROSS-EXAMINATION FINDINGS:
{cross_exam_findings}

---

Review all arguments and make your decision. {"You MUST render a final verdict — no more data requests." if is_final_round else "You may request more data or render a verdict."}"""

    try:
        response, tokens, ms = await call_prism_agent(
            agent_id="CUSTOM_V3_CIO",
            user_message=user_prompt,
            fallback_system_prompt=system_prompt,
            fallback_agent_name="v3_cio",
            temperature=0.2,
            max_tokens=4096,
            priority=Priority.NORMAL,
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
        )

        parsed = parse_json_response(response)
        logger.info(
            "[V3] CIO for %s round %d: %d tokens, %dms, status=%s",
            ticker, round_number, tokens or 0, ms,
            parsed.get("status", parsed.get("action", "?")),
        )

        status = parsed.get("status", "").lower()

        # If CIO requests more data (and not final round)
        if status == "needs_more_data" and not is_final_round:
            data_requests = []
            for dr in parsed.get("data_requests", []):
                if isinstance(dr, dict) and dr.get("description"):
                    try:
                        wt_str = dr.get("worker_type", "worker_fundamental")
                        try:
                            wt = WorkerType(wt_str)
                        except ValueError:
                            wt = WorkerType.FUNDAMENTAL
                        data_requests.append(DataRequest(
                            requesting_manager=ManagerRole.CIO,
                            worker_type=wt,
                            description=dr["description"],
                            priority=dr.get("priority", "critical"),
                            ticker=ticker,
                            specific_metrics=dr.get("specific_metrics", []),
                        ))
                    except Exception:
                        pass

            directed = []
            for dm in parsed.get("directed_managers", []):
                try:
                    directed.append(ManagerRole(dm))
                except ValueError:
                    pass

            return CIODirective(
                status=CIODirectiveStatus.NEEDS_MORE_DATA,
                rationale=parsed.get("rationale", ""),
                data_requests=data_requests,
                directed_managers=directed,
                round_number=round_number,
            )

        # CIO is ready for verdict (or forced on final round)
        raw_action = parsed.get("action", "HOLD").upper()
        action = gate_action(raw_action, held)

        return FamilyOfficeVerdict(
            action=action,
            confidence=int(parsed.get("confidence", 0)),
            winning_side=parsed.get("winning_side", "split"),
            key_deciding_factor=parsed.get("key_deciding_factor", ""),
            rejected_claim_impact=parsed.get("rejected_claim_impact", ""),
            rationale=parsed.get("rationale", ""),
            conviction=parsed.get("conviction", ""),
            original_thesis_status=parsed.get("original_thesis_status", "NOT_HELD" if not held else "VALID"),
            original_thesis_explanation=parsed.get("original_thesis_explanation", ""),
            tokens_used=tokens or 0,
        )

    except Exception as e:
        logger.error("[V3] CIO evaluation failed for %s: %s", ticker, e)
        # On failure, force a conservative verdict
        from app.cognition.debate.action_gate import gate_action
        default_action = gate_action("HOLD", held)
        return FamilyOfficeVerdict(
            action=default_action,
            confidence=0,
            winning_side="split",
            rationale=f"CIO evaluation failed: {e}",
            tokens_used=0,
        )
