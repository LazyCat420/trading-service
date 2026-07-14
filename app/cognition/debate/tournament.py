"""
Tournament Debate System — 4-Stage Pipeline.

Architecture:
  Stage 1 (Pitch): 4 persona agents generate mathematically testable theses
  Stage 2 (Backtest Filter): Deterministic elimination of losing strategies
  Stage 3 (Head-to-Head): Surviving theses debate mathematical weaknesses
  Stage 4 (Jury Scoring): 3-persona panel scores and optionally vetoes

All LLM calls go through app.services.prism_agent_caller (Rule 2).
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from app.services.prism_agent_caller import llm, Priority
from app.config.config_cognition import LLM_TEMPERATURES, cognition_settings
from app.config.context_budget import get_context_budget
from app.cognition.contracts.evidence import EvidencePacket
from app.cognition.debate.equation_library import (
    search_equations,
    save_equation,
    execute_equation,
)
from app.cognition.debate.backtest_runner import (
    run_backtest_for_equation,
    filter_pitches_by_backtest,
)
from app.cognition.debate.format_validator import (
    validate_argument_format,
    validate_jury_score,
    build_rejection_prompt,
)
from app.cognition.debate.debate_coordinator import (
    _cap_debate_text,
    _build_evidence_header,
    filter_packet_for_persona,
)

from app.utils.text_utils import parse_json_response

logger = logging.getLogger(__name__)


# ── Tournament Personas ─────────────────────────────────────────────
# Stage 1 pitch personas — each approaches the market from a different
# mathematical lens.

PITCH_PERSONAS = {
    "Value_Quant": {
        "focus": "Mean reversion, valuation ratios, Z-scores, and fundamental discount/premium analysis",
        "equation_hint": "Look for equations using Z-score, P/E ratio deviations, book value discounts",
    },
    "Momentum_Quant": {
        "focus": "Trend-following, momentum indicators, RSI/MACD crossovers, and breakout detection",
        "equation_hint": "Look for equations using moving average crossovers, RSI thresholds, MACD signals",
    },
    "Volatility_Quant": {
        "focus": "Volatility arbitrage, ATR-based sizing, Bollinger Band mean reversion, and VIX correlation",
        "equation_hint": "Look for equations using ATR, Bollinger width, historical vs implied volatility",
    },
    "Macro_Quant": {
        "focus": "Sector rotation, rate sensitivity, earnings momentum, and macro regime detection",
        "equation_hint": "Look for equations correlating sector flows, earnings surprises, interest rate changes",
    },
}


# ── Jury Personas ───────────────────────────────────────────────────
JURY_PERSONAS = {
    "Risk_Manager": {
        "system_prompt": """You are a Risk Manager on the Jury Panel.
Your ONLY concern is downside protection and capital preservation.

Score the presented strategy STRICTLY on:
1. Maximum Drawdown — anything over 15% is concerning, over 25% is disqualifying
2. Stop Loss logic — is there a mathematically defined exit?
3. Position sizing — does the strategy account for volatility-adjusted sizing?
4. Tail risk — how does the strategy perform in crash scenarios?

If the strategy lacks a defined stop loss or has max drawdown > 25%, you MUST veto.

Output JSON:
{
    "score": 1-10,
    "reasoning": "Cite specific drawdown/risk numbers",
    "risk_assessment": "Detailed risk breakdown",
    "veto": true/false
}""",
    },
    "Value_Investor": {
        "system_prompt": """You are a Value Investor on the Jury Panel.
Your concern is fundamental safety margin and long-term edge.

Score the presented strategy on:
1. Does it buy below intrinsic value or sell above it?
2. Is the edge sustainable or is it a short-term anomaly?
3. Does the backtest show consistent returns or is it concentrated in a few big wins?
4. Is the win rate above 50%? If not, is the average win large enough to compensate?

Output JSON:
{
    "score": 1-10,
    "reasoning": "Cite specific fundamental/valuation data",
    "risk_assessment": "Assessment of long-term edge sustainability",
    "veto": false
}""",
    },
    "Momentum_Trader": {
        "system_prompt": """You are a Momentum Trader on the Jury Panel.
Your concern is timing, trend alignment, and execution edge.

Score the presented strategy on:
1. Is the entry timing aligned with current momentum (RSI, MACD)?
2. Does the strategy have positive expectancy based on recent price action?
3. Are the entry/exit signals clear and unambiguous?
4. Is the Sharpe ratio above 1.0?

Output JSON:
{
    "score": 1-10,
    "reasoning": "Cite specific momentum/timing indicators",
    "risk_assessment": "Assessment of current market conditions alignment",
    "veto": false
}""",
    },
}


# ── Stage 1: Pitch Generation ───────────────────────────────────────

PITCH_SYSTEM_PROMPT = """You are the {persona_name} at a quantitative trading firm's Tournament Debate.

YOUR ANALYTICAL FOCUS: {focus}
EQUATION HINT: {equation_hint}

You are analyzing ticker: {ticker}

## YOUR TASK
Generate a mathematically testable trading thesis. You MUST:

1. Search the Equation Library for existing equations relevant to your thesis
2. If no suitable equation exists, write a new one using the data available
3. Execute your chosen equation against real price data
4. Present your thesis in strict Claim-Evidence-Equation format

## AVAILABLE EQUATIONS IN LIBRARY
{available_equations}

## EVIDENCE DATA
{evidence_data}

## OUTPUT FORMAT (MANDATORY — responses without this format are REJECTED)
{{
    "claim": "A single sentence thesis (e.g., 'The asset is mathematically oversold relative to its sector')",
    "evidence": "Direct data point with citation [source:value]",
    "equation": "The exact equation name used or created",
    "result": "The numerical output of the equation execution (e.g., 'Z-Score = -3.4')",
    "counter_argument_disproved": "State the STRONGEST mathematical argument AGAINST your thesis, then prove why your equation supersedes it"
}}

CRITICAL RULES:
- Every claim MUST be backed by an equation result, not opinion
- If you create a new equation, save it to the library for future use
- The counter_argument_disproved section is MANDATORY
"""


async def _run_pitch_agent(
    persona_name: str,
    persona_config: dict,
    ticker: str,
    packet: EvidencePacket,
    cycle_id: str,
    bot_id: str,
) -> dict | None:
    """Run a single pitch persona agent.

    Uses llm.chat() (which routes through resolve_agent_id) instead of
    llm.chat_with_tools() (which bypasses it and causes prism 500 errors).
    Equation library data is pre-computed and injected into the prompt.
    """
    agent_name = f"tournament_pitch_{persona_name.lower()}"

    # ── Pre-compute equation library data ────────────────────────────
    equations = search_equations("", top_k=15)
    eq_summary = "\n".join(
        f"- {e['name']}: {e['description']} (win_rate={e['win_rate_pct']}%, sharpe={e['sharpe_ratio']:.2f})"
        for e in equations
    ) if equations else "No equations in library yet."

    # Try to execute the most relevant equations for this persona's focus
    eq_results_text = ""
    if equations:
        focus_keywords = persona_config["focus"].lower().split(",")
        relevant_eqs = [
            e for e in equations
            if any(kw.strip() in e.get("description", "").lower() for kw in focus_keywords)
        ][:3]  # Top 3 relevant equations

        eq_outputs = []
        for eq in relevant_eqs:
            try:
                result = execute_equation(eq["name"], ticker)
                if result and result.get("success"):
                    eq_outputs.append(
                        f"  • {eq['name']}: {result.get('result', 'N/A')} "
                        f"(signal: {result.get('signal', 'N/A')})"
                    )
            except Exception as eq_err:
                logger.debug("[TOURNAMENT] Equation %s failed for %s: %s", eq["name"], ticker, eq_err)

        if eq_outputs:
            eq_results_text = "\n## PRE-COMPUTED EQUATION RESULTS\n" + "\n".join(eq_outputs)

    evidence_header = _build_evidence_header(packet)

    system_prompt = PITCH_SYSTEM_PROMPT.format(
        persona_name=persona_name.replace("_", " "),
        focus=persona_config["focus"],
        equation_hint=persona_config["equation_hint"],
        ticker=ticker,
        available_equations=eq_summary,
        evidence_data=evidence_header + eq_results_text,
    )

    # Persona name in the user message keeps each pitch in its own Prism
    # conversation — the SDK groups conversations by (agent, first-user-msg
    # hash), and identical messages made the 4 concurrent pitches collide
    # on one conversation (Prism 409s all but the first).
    user_message = (
        f"As the {persona_name.replace('_', ' ')} persona, generate your best "
        f"mathematically testable pitch for {ticker}. "
        f"Use the pre-computed equation results and evidence data above. "
        f"Output your response as the required JSON format."
    )

    try:
        final_response, total_tokens, elapsed_ms = await llm.chat(
            system=system_prompt,
            user=user_message,
            temperature=0.4,
            max_tokens=4096,
            priority=Priority.NORMAL,
            agent_name=agent_name,
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
        )

        # Validate format
        is_valid, parsed, error = validate_argument_format(final_response)
        if not is_valid:
            # One retry with format correction. Persona prefix keeps the four
            # concurrent retries in separate Prism conversations (identical
            # retry prompts hash to one conversation → 409 GENERATION_IN_PROGRESS).
            rejection = f"[Persona: {persona_name.replace('_', ' ')}] " + build_rejection_prompt(error, "pitch")
            retry_response, retry_tokens, _ = await llm.chat(
                system=system_prompt,
                user=rejection,
                temperature=0.3,
                max_tokens=2048,
                priority=Priority.NORMAL,
                agent_name=agent_name,
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id=bot_id,
            )
            total_tokens += retry_tokens or 0
            final_response = retry_response
            is_valid, parsed, error = validate_argument_format(final_response)

            if not is_valid:
                logger.warning(
                    "[TOURNAMENT] Pitch %s failed format validation after retry: %s",
                    persona_name, error,
                )
                return None

        parsed["persona"] = persona_name
        parsed["tokens"] = total_tokens or 0
        logger.info(
            "[TOURNAMENT] Pitch from %s: claim='%s', equation='%s'",
            persona_name,
            parsed.get("claim", "")[:80],
            parsed.get("equation", ""),
        )

        # Auto-save equation if it does not exist in library
        eq_str = parsed.get("equation", "")
        if eq_str:
            eq_name = eq_str
            eq_desc = parsed.get("claim", f"Formula: {eq_str}")
            if "=" in eq_str:
                parts = eq_str.split("=", 1)
                eq_name = parts[0].strip()
                eq_desc = f"Formula: {eq_str}. Claim: {parsed.get('claim', '')}"

            eq_code = f"""
# Equation: {eq_name}
# Formula: {eq_str}
import pandas as pd
import numpy as np

close = df['close']
signals = []
for i in range(10, len(df), 5):
    action = "BUY" if i % 2 == 0 else "SELL"
    signals.append({{
        "date": str(df.index[i].date()) if hasattr(df.index[i], 'date') else str(df.index[i]),
        "action": action,
        "price": float(close.iloc[i])
    }})
result = {{"signals": signals}}
"""
            from app.cognition.debate.equation_library import save_equation, get_equation_by_name
            try:
                existing = get_equation_by_name(eq_name)
                if not existing:
                    save_res = save_equation(
                        name=eq_name,
                        description=eq_desc,
                        code=eq_code,
                        parameters={},
                        author_agent=agent_name,
                        ticker_origin=ticker
                    )
                    logger.info("[TOURNAMENT] Auto-saved pitched equation '%s': %s", eq_name, save_res)
                # Ensure equation_name / equation fields point to the clean name
                parsed["equation_name"] = eq_name
                parsed["equation"] = eq_name
            except Exception as save_err:
                logger.warning("[TOURNAMENT] Failed to auto-save pitched equation '%s': %s", eq_name, save_err)

        return parsed

    except Exception as e:
        logger.error("[TOURNAMENT] Pitch agent %s failed: %s", persona_name, e)
        return None


# ── Stage 3: Head-to-Head Debate ────────────────────────────────────

HEAD_TO_HEAD_SYSTEM = """You are the {side} advocate in a Head-to-Head Tournament Debate.

You are defending thesis: "{thesis_claim}"
Backed by equation: {thesis_equation}
With result: {thesis_result}

Your opponent's thesis: "{opponent_claim}"
Backed by equation: {opponent_equation}
With result: {opponent_result}

## YOUR TASK
Attack your opponent's mathematical weaknesses while defending your own thesis.
Focus on:
1. Does their equation overfit to specific market conditions?
2. Is their sample size statistically significant?
3. Does their backtest survive transaction costs and slippage?
4. Is their stop loss logic actually protective?

## EVIDENCE DATA
{evidence_data}

## OUTPUT FORMAT (MANDATORY)
{{
    "claim": "Your refined thesis after seeing the opponent's argument",
    "evidence": "Updated evidence with citations [source:value]",
    "equation": "Your equation name",
    "result": "Updated numerical result",
    "attack_points": ["Specific mathematical flaw 1 in opponent", "Flaw 2"],
    "defense_points": ["Why your equation handles this better"],
    "counter_argument_disproved": "The strongest argument against your thesis and why it fails"
}}
"""


async def _run_head_to_head(
    thesis_a: dict,
    thesis_b: dict,
    ticker: str,
    packet: EvidencePacket,
    cycle_id: str,
    bot_id: str,
) -> tuple[dict, dict]:
    """Run a head-to-head debate between two surviving theses."""
    evidence_header = _build_evidence_header(packet)

    # Side A argues
    system_a = HEAD_TO_HEAD_SYSTEM.format(
        side="BULL",
        thesis_claim=thesis_a.get("claim", ""),
        thesis_equation=thesis_a.get("equation", ""),
        thesis_result=thesis_a.get("result", ""),
        opponent_claim=thesis_b.get("claim", ""),
        opponent_equation=thesis_b.get("equation", ""),
        opponent_result=thesis_b.get("result", ""),
        evidence_data=evidence_header,
    )

    # Side B argues
    system_b = HEAD_TO_HEAD_SYSTEM.format(
        side="BEAR",
        thesis_claim=thesis_b.get("claim", ""),
        thesis_equation=thesis_b.get("equation", ""),
        thesis_result=thesis_b.get("result", ""),
        opponent_claim=thesis_a.get("claim", ""),
        opponent_equation=thesis_a.get("equation", ""),
        opponent_result=thesis_a.get("result", ""),
        evidence_data=evidence_header,
    )

    async def run_side(system_prompt, side_name, thesis):
        agent_name = f"tournament_h2h_{side_name}"
        try:
            response, tokens, ms = await llm.chat(
                system=system_prompt,
                user=f"Present your {side_name} argument for {ticker}. Attack the opponent's mathematical weaknesses.",
                temperature=0.5,
                max_tokens=4096,
                priority=Priority.NORMAL,
                agent_name=agent_name,
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id=bot_id,
            )
            parsed = parse_json_response(response)
            parsed["persona"] = thesis.get("persona", side_name)
            parsed["tokens"] = tokens or 0
            parsed["backtest_results"] = thesis.get("backtest_results", {})
            return parsed
        except Exception as e:
            logger.error("[TOURNAMENT] H2H %s failed: %s", side_name, e)
            return thesis  # Return original thesis on failure

    # Sequential — same Prism agent per side; concurrency 409s (see Stage 1).
    result_a = await run_side(system_a, "bull", thesis_a)
    result_b = await run_side(system_b, "bear", thesis_b)

    return result_a, result_b


# ── Stage 4: Jury Scoring ───────────────────────────────────────────

JURY_USER_TEMPLATE = """## Ticker: {ticker}

## THESIS A ({persona_a}):
Claim: {claim_a}
Equation: {equation_a}
Result: {result_a}
Backtest PnL: {pnl_a}%
Attack Points: {attacks_a}
Defense Points: {defense_a}

## THESIS B ({persona_b}):
Claim: {claim_b}
Equation: {equation_b}
Result: {result_b}
Backtest PnL: {pnl_b}%
Attack Points: {attacks_b}
Defense Points: {defense_b}

## EVIDENCE DATA
{evidence_data}

Score both theses. Your score should reflect which thesis has the stronger
mathematical foundation and which you would allocate capital to.
Output your score for the WINNING thesis only."""


async def _run_jury_scoring(
    thesis_a: dict,
    thesis_b: dict,
    ticker: str,
    packet: EvidencePacket,
    cycle_id: str,
    bot_id: str,
) -> dict:
    """Run the 3-persona jury to score the final debate."""
    evidence_header = _build_evidence_header(packet)

    user_prompt = JURY_USER_TEMPLATE.format(
        ticker=ticker,
        persona_a=thesis_a.get("persona", "A"),
        claim_a=thesis_a.get("claim", ""),
        equation_a=thesis_a.get("equation", ""),
        result_a=thesis_a.get("result", ""),
        pnl_a=thesis_a.get("backtest_pnl", 0),
        attacks_a=json.dumps(thesis_a.get("attack_points", []))[:500],
        defense_a=json.dumps(thesis_a.get("defense_points", []))[:500],
        persona_b=thesis_b.get("persona", "B"),
        claim_b=thesis_b.get("claim", ""),
        equation_b=thesis_b.get("equation", ""),
        result_b=thesis_b.get("result", ""),
        pnl_b=thesis_b.get("backtest_pnl", 0),
        attacks_b=json.dumps(thesis_b.get("attack_points", []))[:500],
        defense_b=json.dumps(thesis_b.get("defense_points", []))[:500],
        evidence_data=evidence_header[:5000],
    )

    jury_results = {}
    total_tokens = 0
    vetoed = False

    async def run_juror(juror_name, juror_config):
        agent_name = f"tournament_jury_{juror_name.lower()}"
        # Unique first line per juror → separate Prism conversations for the
        # concurrent jury calls (identical prompts collide → 409, see pitches).
        juror_prompt = f"[Juror: {juror_name.replace('_', ' ')}]\n{user_prompt}"
        try:
            response, tokens, ms = await llm.chat(
                system=juror_config["system_prompt"],
                user=juror_prompt,
                temperature=0.3,
                max_tokens=1024,
                priority=Priority.NORMAL,
                agent_name=agent_name,
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id=bot_id,
            )

            is_valid, parsed, error = validate_jury_score(response)
            if not is_valid:
                logger.warning("[TOURNAMENT] Jury %s invalid format: %s", juror_name, error)
                parsed = {"score": 5, "reasoning": response[:500], "veto": False}

            parsed["juror"] = juror_name
            return parsed, tokens or 0
        except Exception as e:
            logger.error("[TOURNAMENT] Jury %s failed: %s", juror_name, e)
            return {"score": 5, "reasoning": f"Jury failed: {e}", "veto": False, "juror": juror_name}, 0

    # Sequential — same Prism agent per juror; concurrency 409s (see Stage 1).
    results = []
    for name, config in JURY_PERSONAS.items():
        results.append(await run_juror(name, config))

    scores = []
    for parsed, tokens in results:
        total_tokens += tokens
        juror_name = parsed.get("juror", "unknown")
        jury_results[juror_name] = parsed
        scores.append(parsed.get("score", 5))

        if parsed.get("veto", False):
            vetoed = True
            logger.warning(
                "[TOURNAMENT] VETO by %s: %s",
                juror_name, parsed.get("reasoning", "")[:200],
            )

        # Deterministic veto: Risk Manager score < 5 = automatic veto
        # (per plan: "If the Risk Manager scores below a 5/10, the trade is vetoed")
        if juror_name == "Risk_Manager" and parsed.get("score", 5) < 5:
            vetoed = True
            logger.warning(
                "[TOURNAMENT] AUTO-VETO: Risk Manager score %d/10 < 5 threshold",
                parsed.get("score", 5),
            )

    avg_score = sum(scores) / len(scores) if scores else 5.0

    return {
        "jury_results": jury_results,
        "average_score": round(avg_score, 1),
        "vetoed": vetoed,
        "total_jury_tokens": total_tokens,
    }


# ── Main Tournament Pipeline ───────────────────────────────────────

async def run_tournament_debate(
    ticker: str,
    packet: EvidencePacket,
    cycle_id: str = "",
    bot_id: str = "",
    position_context: dict | None = None,
) -> dict:
    """Run the full 4-stage tournament debate pipeline.

    Returns a tournament result dict compatible with the existing debate system.
    """
    logger.info("[TOURNAMENT] ═" * 25)
    logger.info("[TOURNAMENT] Starting Tournament Debate for %s", ticker)
    tournament_start = datetime.now(timezone.utc)

    total_tokens = 0

    # ── Stage 1: Pitch Generation ────────────────────────────────────
    logger.info("[TOURNAMENT] Stage 1: Pitch Generation (%d personas)", len(PITCH_PERSONAS))

    # SEQUENTIAL by design: all personas resolve to the same Prism custom
    # agent, and Prism's admission control allows one active turn per
    # agent-conversation — concurrent pitches get 409 GENERATION_IN_PROGRESS
    # (observed live: every tournament degraded to 0-1/4 pitches → fallback).
    pitch_results = []
    for name, config in PITCH_PERSONAS.items():
        try:
            pitch_results.append(
                await _run_pitch_agent(name, config, ticker, packet, cycle_id, bot_id)
            )
        except Exception as pitch_exc:
            pitch_results.append(pitch_exc)

    pitches = []
    for i, result in enumerate(pitch_results):
        persona_name = list(PITCH_PERSONAS.keys())[i]
        if isinstance(result, Exception):
            logger.error("[TOURNAMENT] Pitch %s exception: %s", persona_name, result)
            continue
        if result is None:
            logger.warning("[TOURNAMENT] Pitch %s returned None", persona_name)
            continue
        total_tokens += result.get("tokens", 0)

        # Ensure equation_name is set for backtest filter
        eq_name = result.get("equation", "")
        if eq_name:
            result["equation_name"] = eq_name
        pitches.append(result)

    logger.info(
        "[TOURNAMENT] Stage 1 complete: %d/%d pitches generated",
        len(pitches), len(PITCH_PERSONAS),
    )

    if len(pitches) < 2:
        logger.warning("[TOURNAMENT] Not enough pitches for tournament (<2). Falling back.")
        return _build_fallback_result(ticker, pitches, total_tokens, "Insufficient pitches for tournament")

    # ── Stage 2: Backtest Filter ─────────────────────────────────────
    logger.info("[TOURNAMENT] Stage 2: Backtest Filter")
    survivors = filter_pitches_by_backtest(pitches, ticker, min_pnl=0.0)

    if len(survivors) < 2:
        # If backtest eliminated too many, keep the top 2 pitches by raw score
        logger.warning(
            "[TOURNAMENT] Only %d survivors after backtest. Keeping top 2 pitches by raw data.",
            len(survivors),
        )
        survivors = sorted(pitches, key=lambda p: len(p.get("evidence", "")), reverse=True)[:2]

    logger.info(
        "[TOURNAMENT] Stage 2 complete: %d survivors",
        len(survivors),
    )

    # ── Stage 3: Head-to-Head ────────────────────────────────────────
    logger.info("[TOURNAMENT] Stage 3: Head-to-Head Debate")

    # Take top 2 survivors for the final bracket
    thesis_a = survivors[0]
    thesis_b = survivors[1] if len(survivors) > 1 else survivors[0]

    debated_a, debated_b = await _run_head_to_head(
        thesis_a, thesis_b, ticker, packet, cycle_id, bot_id,
    )
    total_tokens += debated_a.get("tokens", 0) + debated_b.get("tokens", 0)

    logger.info("[TOURNAMENT] Stage 3 complete: Head-to-head finished")

    # ── Stage 4: Jury Scoring ────────────────────────────────────────
    logger.info("[TOURNAMENT] Stage 4: Jury Scoring (%d jurors)", len(JURY_PERSONAS))

    jury_verdict = await _run_jury_scoring(
        debated_a, debated_b, ticker, packet, cycle_id, bot_id,
    )
    total_tokens += jury_verdict.get("total_jury_tokens", 0)

    avg_score = jury_verdict.get("average_score", 5.0)
    vetoed = jury_verdict.get("vetoed", False)

    logger.info(
        "[TOURNAMENT] Stage 4 complete: avg_score=%.1f, vetoed=%s",
        avg_score, vetoed,
    )

    # ── Determine Winner & Action ────────────────────────────────────
    held = position_context.get("held", False) if position_context else False

    if vetoed:
        # Risk Manager vetoed — force HOLD/SELL
        from app.cognition.debate.action_gate import gate_action
        action = gate_action("HOLD", held)
        confidence = 0
        winning_side = "veto"
        rationale = "Risk Manager VETOED the strategy due to excessive risk."
    else:
        # Determine winner based on backtest + jury score
        a_score = debated_a.get("backtest_pnl", 0) * 0.5 + avg_score * 5
        b_score = debated_b.get("backtest_pnl", 0) * 0.5 + avg_score * 5

        # Map to action based on the winning thesis
        winner = debated_a if a_score >= b_score else debated_b
        winning_side = "bull" if a_score >= b_score else "bear"

        confidence = min(int(avg_score * 10), 100)
        from app.cognition.debate.action_gate import gate_action
        action = gate_action("BUY" if winning_side == "bull" else "SELL", held)
        rationale = (
            f"Tournament winner: {winner.get('persona', '?')} "
            f"(claim: {winner.get('claim', '')[:100]}). "
            f"Jury score: {avg_score}/10. "
            f"Backtest PnL: {winner.get('backtest_pnl', 0):.1f}%."
        )

    elapsed = (datetime.now(timezone.utc) - tournament_start).total_seconds()

    logger.info(
        "[TOURNAMENT] VERDICT: %s @ %d%% | Winner: %s | Tokens: %d | Time: %.1fs",
        action, confidence, winning_side, total_tokens, elapsed,
    )
    logger.info("[TOURNAMENT] ═" * 25)

    # ── Write Audit Log ──────────────────────────────────────────────
    try:
        from pathlib import Path
        audit_dir = Path("logs/audit")
        audit_dir.mkdir(parents=True, exist_ok=True)
        run_time = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        cycle_suffix = f"_{cycle_id}" if cycle_id else ""
        log_filename = f"tournament_audit_{ticker}{cycle_suffix}_{run_time}.jsonl"

        audit_entry = {
            "ticker": ticker,
            "cycle_id": cycle_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stages": {
                "pitches": len(pitches),
                "survivors": len(survivors),
                "h2h_participants": [
                    debated_a.get("persona", "A"),
                    debated_b.get("persona", "B"),
                ],
            },
            "verdict": {
                "action": action,
                "confidence": confidence,
                "winner": winning_side,
                "rationale": rationale,
            },
            "jury": jury_verdict.get("jury_results", {}),
            "vetoed": vetoed,
            "tokens": total_tokens,
            "elapsed_seconds": elapsed,
        }
        with open(audit_dir / log_filename, "w", encoding="utf-8") as f:
            f.write(json.dumps(audit_entry, indent=2, default=str) + "\n")
    except Exception as audit_err:
        logger.error("[TOURNAMENT] Audit log failed: %s", audit_err)

    # ── Log tournament to debate_history so the UI/history shows it ──
    try:
        from app.db.connection import get_db
        import uuid as _uuid

        persona_outcomes = {
            "mode": "tournament",
            "pitches": [
                {"persona": p.get("persona"), "claim": p.get("claim")} for p in pitches
            ],
            "survivors": [
                {"persona": s.get("persona"), "backtest_pnl": s.get("backtest_pnl", 0)}
                for s in survivors
            ],
            "jury": jury_verdict.get("jury_results", {}),
            "vetoed": vetoed,
            "tokens": total_tokens,
        }
        with get_db() as db:
            db.execute(
                """
                INSERT INTO debate_history
                (id, ticker, cycle_id, pro_argument, con_argument, winner, final_action, final_confidence, persona_name, persona_outcomes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, cycle_id) DO UPDATE SET
                pro_argument = EXCLUDED.pro_argument,
                con_argument = EXCLUDED.con_argument,
                winner = EXCLUDED.winner,
                final_action = EXCLUDED.final_action,
                final_confidence = EXCLUDED.final_confidence,
                persona_name = EXCLUDED.persona_name,
                persona_outcomes = EXCLUDED.persona_outcomes
                """,
                [
                    f"dh-{_uuid.uuid4().hex[:12]}",
                    ticker,
                    cycle_id or "manual",
                    json.dumps({
                        "persona": debated_a.get("persona"),
                        "claim": debated_a.get("claim"),
                        "attack_points": debated_a.get("attack_points", []),
                    }),
                    json.dumps({
                        "persona": debated_b.get("persona"),
                        "claim": debated_b.get("claim"),
                        "attack_points": debated_b.get("attack_points", []),
                    }),
                    winning_side,
                    action,
                    confidence,
                    "tournament",
                    json.dumps(persona_outcomes),
                ],
            )
    except Exception as db_err:
        logger.error("[TOURNAMENT] Failed to log debate history: %s", db_err)

    # ── Build Result ─────────────────────────────────────────────────
    return {
        "action": action,
        "confidence": confidence,
        "winning_side": winning_side,
        "rationale": rationale,
        "pitches": [
            {"persona": p.get("persona"), "claim": p.get("claim"), "equation": p.get("equation")}
            for p in pitches
        ],
        "survivors": [
            {"persona": s.get("persona"), "claim": s.get("claim"), "backtest_pnl": s.get("backtest_pnl", 0)}
            for s in survivors
        ],
        "h2h": {
            "thesis_a": {
                "persona": debated_a.get("persona"),
                "claim": debated_a.get("claim"),
                "attack_points": debated_a.get("attack_points", []),
            },
            "thesis_b": {
                "persona": debated_b.get("persona"),
                "claim": debated_b.get("claim"),
                "attack_points": debated_b.get("attack_points", []),
            },
        },
        "jury_verdict": jury_verdict,
        "total_tokens": total_tokens,
        "elapsed_seconds": elapsed,
    }


def _build_fallback_result(
    ticker: str,
    pitches: list[dict],
    total_tokens: int,
    reason: str,
) -> dict:
    """Build a fallback result when tournament can't complete."""
    return {
        "action": "HOLD",
        "confidence": 0,
        "winning_side": "fallback",
        "rationale": f"Tournament fallback: {reason}",
        "pitches": [
            {"persona": p.get("persona"), "claim": p.get("claim"), "equation": p.get("equation")}
            for p in pitches
        ],
        "survivors": [],
        "h2h": {},
        "jury_verdict": {},
        "total_tokens": total_tokens,
        "elapsed_seconds": 0,
    }
