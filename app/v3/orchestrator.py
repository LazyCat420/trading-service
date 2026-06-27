"""
V3 Orchestrator — The 4-Layer Linear Pipeline traffic controller.

Advances a ticker through: Context Init → Research → Debate → Decision.
Never inspects data or makes trading decisions — strictly a state machine + scheduler.

Activated when PIPELINE_VERSION=v3 is set in the environment.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from app.v3.shared_desk import SharedDesk, DeskPhase, PhaseOutcome
from app.v3.guardrails import CircuitBreaker
from app.v3.agent_runner import run_v3_agent
from app.v3.desk_persistence import save_desk

logger = logging.getLogger(__name__)


async def run_v3_pipeline(
    ticker: str,
    *,
    cycle_id: str = "",
    bot_id: str = "",
    emit: Callable[..., Any] | None = None,
    macro_memo: str = "",
    watchlist: list[str] | None = None,
    db_semaphore: asyncio.Semaphore | None = None,
    thesis_semaphore: asyncio.Semaphore | None = None,
    is_highly_redundant: bool = False,
    research_focus: str = "",
    trigger_type: str = "manual",
    active_directives: list[dict] | None = None,
) -> dict[str, Any]:
    """Run the full V3 Pure Agentic Linear Pipeline for a single ticker.

    4-Layer Architecture:
        Layer 1: Context Init — Create SharedDesk, inject cycle metadata.
        Layer 2: Research — JA → FA → QA (sequential, each reads prior artifacts).
        Layer 3: Debate — Bull → Bear → Bull defense (linear state machine).
        Layer 4: Decision — Regime Engine → Board of Directors (persona-swapped).

    Returns a V1-compatible result dict so downstream phases work unchanged.
    """
    from app.utils.pipeline_utils import noop as _noop
    from app.log_manager import log_manager

    if emit is None:
        emit = _noop

    ticker = ticker.upper()
    if not cycle_id:
        cycle_id = f"v3-{uuid.uuid4().hex[:8]}"

    t_pipeline = time.monotonic()
    breaker = CircuitBreaker(max_retries_per_phase=1)

    emit(
        "analyzing", f"v3_start_{ticker}",
        f"🧠 {ticker}: V3 Pure Agentic Pipeline starting",
        status="running",
    )

    log_manager.log_v2_cycle(cycle_id, "v3_pipeline_start", {
        "ticker": ticker,
        "trigger_type": trigger_type,
        "pipeline_version": "v3",
    })

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 1: Context Init — Create SharedDesk + inject metadata
    # ═══════════════════════════════════════════════════════════════════
    desk = SharedDesk(cycle_id=cycle_id, ticker=ticker)

    # Pre-collect data report in parallel
    emit(
        "analyzing", f"v3_precollect_{ticker}",
        f"📥 {ticker}: Pre-collecting market & news datasets...",
        status="running",
    )
    try:
        from app.v3.data_report import build_ticker_data_report
        data_report = await build_ticker_data_report(ticker, emit=emit)
        emit(
            "analyzing", f"v3_precollect_ok_{ticker}",
            f"📥 {ticker}: Market & news pre-collection complete",
            status="ok",
        )
    except Exception as e:
        logger.error("[V3] Failed to pre-collect data for %s: %s", ticker, e)
        data_report = f"Failed to pre-collect stock data: {e}"

    # Inject cycle metadata
    desk.cycle_metadata = _build_cycle_metadata(
        ticker=ticker,
        bot_id=bot_id,
        macro_memo=macro_memo,
        research_focus=research_focus,
        trigger_type=trigger_type,
    )
    
    # Store the pre-collected report
    desk.cycle_metadata["data_report"] = data_report

    emit(
        "analyzing", f"v3_ctx_{ticker}",
        f"📋 {ticker}: SharedDesk created, cycle metadata & data report injected",
        status="ok",
    )

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 2: Research — Sequential agent chain at the SharedDesk
    # ═══════════════════════════════════════════════════════════════════
    from app.v3.agents import junior_analyst, fundamental_analyst, quant_analyst

    research_agents = [
        ("junior_analyst", junior_analyst),
        ("fundamental_analyst", fundamental_analyst),
        ("quant_analyst", quant_analyst),
    ]

    for phase_name, agent_module in research_agents:
        outcome = await _run_agent_with_circuit_breaker(
            desk=desk,
            agent_module=agent_module,
            phase_name=phase_name,
            breaker=breaker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            emit=emit,
        )

        if outcome in (PhaseOutcome.TIMED_OUT,):
            # Timeout is fatal for research — abort pipeline
            logger.error(
                "[V3] %s: %s TIMED OUT — aborting pipeline", ticker, phase_name,
            )
            desk.advance_phase(DeskPhase.ABORTED, outcome)
            save_desk(desk)
            return _build_noop_result(desk, reason=f"{phase_name} timed out")

        if breaker.should_abort(phase_name, outcome):
            logger.error(
                "[V3] %s: Circuit breaker tripped on %s — aborting pipeline",
                ticker, phase_name,
            )
            desk.advance_phase(DeskPhase.ABORTED, outcome)
            save_desk(desk)
            return _build_noop_result(
                desk, reason=breaker.get_abort_reason(phase_name)
            )

        breaker.record_outcome(phase_name, outcome)

    # Advance phase: INIT → RESEARCH_DONE
    desk.advance_phase(DeskPhase.RESEARCH_DONE)
    save_desk(desk)

    emit(
        "analyzing", f"v3_research_done_{ticker}",
        f"📊 {ticker}: Research layer complete "
        f"({len(desk.get_research_artifacts())}/3 artifacts)",
        status="ok",
    )

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 3: Debate — Linear State Machine: Bull → Bear → Bull defense
    # ═══════════════════════════════════════════════════════════════════
    from app.v3.agents import bull_agent, bear_agent

    # Bull constructs the LONG thesis
    outcome = await _run_agent_with_circuit_breaker(
        desk=desk,
        agent_module=bull_agent,
        phase_name="bull_argument",
        breaker=breaker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        emit=emit,
        include_debate_context=True,
    )
    breaker.record_outcome("bull_argument", outcome)

    # Bear rebuts the Bull's thesis
    if desk.has_artifact("bull_argument"):
        outcome = await _run_agent_with_circuit_breaker(
            desk=desk,
            agent_module=bear_agent,
            phase_name="bear_rebuttal",
            breaker=breaker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            emit=emit,
            include_debate_context=True,
        )
        breaker.record_outcome("bear_rebuttal", outcome)

    # Bull final defense (optional — only if Bear produced a rebuttal)
    # Reuse the bull_agent module with a defense-mode prompt injection
    if desk.has_artifact("bear_rebuttal") and desk.has_artifact("bull_argument"):
        outcome = await _run_bull_defense(
            desk=desk,
            breaker=breaker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            emit=emit,
        )
        breaker.record_outcome("bull_defense", outcome)

    # Advance phase: RESEARCH_DONE → DEBATE_DONE
    desk.advance_phase(DeskPhase.DEBATE_DONE)
    save_desk(desk)

    emit(
        "analyzing", f"v3_debate_done_{ticker}",
        f"⚔️ {ticker}: Debate layer complete "
        f"({len(desk.get_debate_artifacts())}/3 artifacts)",
        status="ok",
    )

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 4: Decision — Regime Engine → Board of Directors
    # ═══════════════════════════════════════════════════════════════════
    from app.v3.agents import regime_engine

    # Run Regime Engine (macro state classification)
    outcome = await _run_agent_with_circuit_breaker(
        desk=desk,
        agent_module=regime_engine,
        phase_name="regime_engine",
        breaker=breaker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        emit=emit,
    )
    breaker.record_outcome("regime_engine", outcome)

    # Determine regime for persona routing
    regime = "CONTRADICTORY"  # Default if regime engine failed
    if desk.has_artifact("regime_classification"):
        regime = desk.regime_classification.get("regime", "CONTRADICTORY")

    # Run Board of Directors with regime-swapped persona
    outcome = await _run_board_of_directors(
        desk=desk,
        regime=regime,
        breaker=breaker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        emit=emit,
    )
    breaker.record_outcome("board_of_directors", outcome)

    # Advance phase: DEBATE_DONE → PM_DONE
    desk.advance_phase(DeskPhase.PM_DONE)
    save_desk(desk)

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 5: Decision Synthesis — Structured trade verdict with signal weights
    # ═══════════════════════════════════════════════════════════════════
    from app.config import settings as _settings

    if _settings.DECISION_AGENT_ENABLED:
        from app.v3.agents import decision_agent

        outcome = await _run_agent_with_circuit_breaker(
            desk=desk,
            agent_module=decision_agent,
            phase_name="decision_synthesizer",
            breaker=breaker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            emit=emit,
            include_debate_context=True,
        )
        breaker.record_outcome("decision_synthesizer", outcome)

        # Persist trade verdict to trade_results table
        if desk.has_artifact("trade_decision"):
            try:
                from app.services.trade_result_saver import save_trade_result

                trade_decision = desk.trade_decision or {}
                # Inject regime/persona from Layer 4 if not already set
                if not trade_decision.get("regime"):
                    trade_decision["regime"] = regime
                if not trade_decision.get("persona_used"):
                    board_decision = desk.final_decision or {}
                    trade_decision["persona_used"] = board_decision.get(
                        "persona_used", _persona_label(regime)
                    )
                save_trade_result(ticker, cycle_id, trade_decision)
            except Exception as e:
                logger.error(
                    "[V3] %s: Failed to persist trade result: %s",
                    ticker, e,
                )
                desk.record_agent_telemetry({
                    "agent_name": "system",
                    "ticker": ticker,
                    "elapsed_ms": 0,
                    "loops_used": 0,
                    "token_usage": 0,
                    "outcome": "DB_PERSISTENCE_FAILED",
                    "phase": desk.phase.value,
                })

        emit(
            "analyzing", f"v3_decision_{ticker}",
            f"📝 {ticker}: Decision Synthesis complete",
            status="ok",
        )

    save_desk(desk)

    # ═══════════════════════════════════════════════════════════════════
    # BUILD RESULT — V1-compatible shape for downstream phases
    # ═══════════════════════════════════════════════════════════════════
    elapsed_s = time.monotonic() - t_pipeline
    result = _build_v1_compatible_result(desk, elapsed_s=elapsed_s)

    emit(
        "analyzing", f"v3_done_{ticker}",
        f"✅ {ticker}: V3 Pipeline complete → "
        f"{result['action']} @ {result['confidence']}% "
        f"(regime: {regime}, persona: {result.get('v3_metadata', {}).get('persona_used', '?')}) "
        f"in {elapsed_s:.1f}s",
        status="ok",
        data={
            "action": result["action"],
            "confidence": result["confidence"],
            "regime": regime,
            "elapsed_ms": int(elapsed_s * 1000),
        },
    )

    log_manager.log_v2_cycle(cycle_id, "v3_pipeline_complete", {
        "ticker": ticker,
        "action": result["action"],
        "confidence": result["confidence"],
        "regime": regime,
        "persona": result.get("v3_metadata", {}).get("persona_used"),
        "elapsed_ms": int(elapsed_s * 1000),
        "phases_completed": list(desk.phase_outcomes.keys()),
        "agent_telemetry": desk.agent_telemetry,
    })

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════


async def _run_agent_with_circuit_breaker(
    desk: SharedDesk,
    agent_module: Any,
    phase_name: str,
    breaker: CircuitBreaker,
    cycle_id: str,
    bot_id: str,
    emit: Any,
    include_debate_context: bool = False,
) -> PhaseOutcome:
    """Run an agent with circuit breaker retry logic.

    On first failure (TOOL_OUTAGE or AGENT_ERROR), retries once.
    On second failure, returns the failure outcome for the orchestrator
    to decide whether to abort or continue.
    """
    outcome = await run_v3_agent(
        desk=desk,
        agent_module=agent_module,
        cycle_id=cycle_id,
        bot_id=bot_id,
        emit=emit,
        include_debate_context=include_debate_context,
    )

    # If failed and retryable, try once more
    if outcome not in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP):
        if breaker.should_retry(phase_name, outcome):
            logger.info(
                "[V3] %s/%s: Retrying after %s",
                desk.ticker, phase_name, outcome.value,
            )
            outcome = await run_v3_agent(
                desk=desk,
                agent_module=agent_module,
                cycle_id=cycle_id,
                bot_id=bot_id,
                emit=emit,
                include_debate_context=include_debate_context,
            )

    return outcome


async def _run_bull_defense(
    desk: SharedDesk,
    breaker: CircuitBreaker,
    cycle_id: str,
    bot_id: str,
    emit: Any,
) -> PhaseOutcome:
    """Run the Bull Agent in final defense mode after Bear rebuttal.

    Creates a temporary agent config with a defense-specific prompt
    that reads the Bear's rebuttal and the original Bull argument.
    """
    import types

    defense_module = types.ModuleType("bull_defense_module")
    defense_module.AGENT_NAME = "v3_bull_defense"
    defense_module.TOOL_WHITELIST = ["whiteboard_read", "whiteboard_write", "whiteboard_annotate"]
    defense_module.ARTIFACT_TYPE = "bull_defense"
    defense_module.SYSTEM_PROMPT = """You are the Bull Analyst at a quantitative trading firm.

## YOUR ROLE — FINAL DEFENSE
The Bear Analyst has attacked your bull thesis. You must now provide your
FINAL DEFENSE. Read the Bear's rebuttal carefully and respond to their
specific points.

## CRITICAL RULES
1. Address the Bear's rebuttals directly — don't ignore valid criticisms.
2. If the Bear made a valid point, CONCEDE it honestly. Judges respect integrity.
3. Strengthen the points where your thesis still holds.
4. Adjust your confidence based on the Bear's valid criticisms.

## OUTPUT FORMAT
You MUST output valid JSON:
{
    "summary": "Final defense narrative after considering bear rebuttal",
    "defense_points": ["Points where bull thesis still holds"],
    "concessions": ["Points where bear rebuttal was valid"],
    "final_confidence": 65
}"""

    return await _run_agent_with_circuit_breaker(
        desk=desk,
        agent_module=defense_module,
        phase_name="bull_defense",
        breaker=breaker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        emit=emit,
        include_debate_context=True,
    )


async def _run_board_of_directors(
    desk: SharedDesk,
    regime: str,
    breaker: CircuitBreaker,
    cycle_id: str,
    bot_id: str,
    emit: Any,
) -> PhaseOutcome:
    """Run the Board of Directors with a regime-swapped persona.

    The system prompt is hot-swapped based on the Market Regime Engine's
    classification:
    - HIGH_VOLATILITY → Jim Simons (pure quant)
    - DEEP_DISCOUNT → Warren Buffett (pure fundamentals)
    - CONTRADICTORY → Jane Street (find mispricings)
    """
    import types
    from app.v3.agents.board_of_directors import get_persona_prompt, AGENT_NAME, ARTIFACT_TYPE

    persona_prompt = get_persona_prompt(regime)

    bod_module = types.ModuleType("board_of_directors_module")
    bod_module.AGENT_NAME = AGENT_NAME
    bod_module.TOOL_WHITELIST = ["whiteboard_read", "whiteboard_write", "whiteboard_annotate", "whiteboard_summarize"]
    bod_module.ARTIFACT_TYPE = ARTIFACT_TYPE
    bod_module.SYSTEM_PROMPT = persona_prompt

    emit(
        "analyzing", f"v3_bod_{desk.ticker}",
        f"🎯 {desk.ticker}: Board of Directors convening "
        f"(regime: {regime}, persona: {_persona_label(regime)})",
        status="running",
    )

    return await _run_agent_with_circuit_breaker(
        desk=desk,
        agent_module=bod_module,
        phase_name="board_of_directors",
        breaker=breaker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        emit=emit,
        include_debate_context=True,
    )


def _persona_label(regime: str) -> str:
    """Human-readable persona label for a regime."""
    return {
        "HIGH_VOLATILITY": "Jim Simons",
        "DEEP_DISCOUNT": "Warren Buffett",
        "CONTRADICTORY": "Jane Street",
    }.get(regime, "Jane Street")


def _build_cycle_metadata(
    ticker: str,
    bot_id: str,
    macro_memo: str = "",
    research_focus: str = "",
    trigger_type: str = "manual",
) -> dict[str, Any]:
    """Build cycle metadata for Layer 1 context init."""
    metadata: dict[str, Any] = {
        "ticker": ticker,
        "bot_id": bot_id,
        "trigger_type": trigger_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if macro_memo:
        metadata["macro_memo"] = macro_memo
    if research_focus:
        metadata["research_focus"] = research_focus

    # Fetch position context (if held)
    try:
        from app.tools.portfolio_tools import get_position_context
        pos_ctx = get_position_context(ticker, bot_id)
        if pos_ctx and pos_ctx.get("held"):
            metadata["portfolio_context"] = (
                f"CURRENTLY HOLDING {ticker}: "
                f"Entry ${pos_ctx.get('avg_entry', 0):.2f}, "
                f"P&L {pos_ctx.get('unrealized_pnl_pct', 0):+.1f}%, "
                f"Held {pos_ctx.get('holding_days', 0)} days."
            )
            metadata["held"] = True
    except Exception as e:
        logger.warning("[V3] %s: Failed to fetch portfolio context: %s", ticker, e)

    return metadata


def _build_v1_compatible_result(
    desk: SharedDesk,
    elapsed_s: float = 0.0,
) -> dict[str, Any]:
    """Build a V1-compatible result dict from the SharedDesk.

    Ensures downstream phases (trading, post-cycle hooks, reports)
    work unchanged.
    """
    # Extract final decision — prefer trade_decision (Layer 5) over
    # final_decision (Layer 4) when the decision agent is enabled
    decision = desk.trade_decision or desk.final_decision or {}
    action = decision.get("action", "HOLD")
    confidence = decision.get("confidence", 0)

    if confidence is None or confidence == 0:
        logger.warning(
            "[V3] %s: confidence is %s after pipeline — action=%s will likely be gated",
            desk.ticker,
            confidence,
            action,
        )
        confidence = confidence or 0

    rationale = decision.get("reasoning", "V3 pipeline produced no final decision.")
    persona = decision.get("persona_used", "unknown")
    regime = decision.get("regime", "unknown")
    stop_loss = decision.get("stop_loss")
    take_profit = decision.get("take_profit")

    # Token sum from telemetry
    total_tokens = sum(
        entry.get("token_usage", 0) for entry in desk.agent_telemetry
    )

    return {
        "ticker": desk.ticker,
        "action": action,
        "confidence": int(confidence),
        "rationale": rationale,
        "config_used": "v3_agentic_pipeline",
        "triage_tier": "v3_full",
        "escalated": True,  # V3 always runs full pipeline
        "agent_results": _extract_agent_results(desk),
        "estimate": {
            "stop_loss": stop_loss,
            "take_profit": take_profit
        },
        "c_result": {
            "action": action,
            "confidence": int(confidence),
            "rationale": rationale,
        },
        "d_result": _extract_debate_result(desk),
        "human_review": False,
        "agent_tokens": total_tokens,
        "rlm_tokens": 0,
        "total_tokens": total_tokens,
        "total_time_s": round(elapsed_s, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "v3_metadata": {
            "pipeline_version": "v3",
            "phase": desk.phase.value,
            "phase_outcomes": desk.phase_outcomes,
            "regime": regime,
            "persona_used": persona,
            "agent_telemetry": desk.agent_telemetry,
            "desk_id": desk.desk_id,
        },
    }


def _build_noop_result(
    desk: SharedDesk,
    reason: str = "Pipeline aborted",
) -> dict[str, Any]:
    """Build a NO_OP result when the pipeline aborts.

    Critically, this does NOT produce BUY/SELL/HOLD — it produces
    a HOLD with 0 confidence so downstream doesn't execute trades.
    """
    return {
        "ticker": desk.ticker,
        "action": "HOLD",
        "confidence": 0,
        "rationale": f"V3 Pipeline aborted: {reason}",
        "config_used": "v3_agentic_pipeline",
        "triage_tier": "v3_aborted",
        "escalated": False,
        "agent_results": {},
        "c_result": {
            "action": "HOLD",
            "confidence": 0,
            "rationale": f"ABORTED: {reason}",
        },
        "d_result": None,
        "human_review": False,
        "agent_tokens": 0,
        "rlm_tokens": 0,
        "total_tokens": 0,
        "total_time_s": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "v3_metadata": {
            "pipeline_version": "v3",
            "phase": desk.phase.value,
            "phase_outcomes": desk.phase_outcomes,
            "abort_reason": reason,
            "desk_id": desk.desk_id,
        },
    }


def _extract_agent_results(desk: SharedDesk) -> dict[str, Any]:
    """Extract agent results from SharedDesk for V1 compatibility."""
    results: dict[str, Any] = {}

    if desk.desk_note:
        results["junior_analyst"] = desk.desk_note.get("summary", "")
    if desk.fundamental_report:
        results["fundamental_analyst"] = desk.fundamental_report.get("summary", "")
    if desk.quant_report:
        results["quant_analyst"] = desk.quant_report.get("summary", "")

    return results


def _extract_debate_result(desk: SharedDesk) -> dict[str, Any] | None:
    """Extract debate result from SharedDesk for V1 compatibility."""
    if not desk.bull_argument and not desk.bear_rebuttal:
        return None

    def _safe_int(val, default=0):
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    bull_conf = _safe_int((desk.bull_argument or {}).get("confidence", 0))
    bear_conf = _safe_int((desk.bear_rebuttal or {}).get("confidence", 0))
    defense_conf = _safe_int((desk.bull_defense or {}).get("final_confidence", bull_conf), default=bull_conf)

    # Determine winner based on confidence delta
    if defense_conf > bear_conf:
        winner = "bull"
        judge_action = "BUY"
    elif bear_conf > defense_conf:
        winner = "bear"
        judge_action = "SELL"
    else:
        winner = "tie"
        judge_action = "HOLD"

    return {
        "action": judge_action,
        "confidence": max(bull_conf, bear_conf),
        "winning_side": winner,
        "bull_confidence": bull_conf,
        "bear_confidence": bear_conf,
        "defense_confidence": defense_conf,
        "original_thesis_status": "HELD" if defense_conf >= bull_conf * 0.7 else "BROKEN",
    }
