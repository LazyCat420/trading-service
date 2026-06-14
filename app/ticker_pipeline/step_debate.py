"""
Step: Adversarial Debate.

Runs Bull vs Bear adversarial debate using debate_coordinator.
Includes budget-aware skip logic — if the pipeline has consumed too much
time, debate is skipped to preserve budget for thesis generation.

Extracted from runner.py Steps 5.7.
"""

import asyncio
import logging
import time

from app.ticker_pipeline.context import TickerContext
from app.log_manager import log_manager

logger = logging.getLogger(__name__)

# Thesis retry loop worst-case: 180s + 30s cooldown + 120s = 330s
# Hallucination check + memory write + DB log ≈ 30s
_THESIS_BUDGET_SECONDS = 360


async def run_debate_step(ctx: TickerContext) -> TickerContext:
    """Run adversarial debate if conditions are met."""
    from app.cycle.orchestration.cycle_control import cycle_control

    # ── Skip conditions ──
    skip_debate = not ctx.orchestrator_had_agents

    if not skip_debate:
        from app.config import settings as _settings
        _elapsed = ctx.elapsed_s()
        _remaining = float(_settings.ANALYSIS_WORKER_TIMEOUT_SECONDS) - _elapsed
        if _remaining < (_THESIS_BUDGET_SECONDS + 300):
            skip_debate = True
            logger.warning(
                "[V2] Skipping debate for %s — only %.0fs remaining in worker budget",
                ctx.ticker, _remaining,
            )
            ctx.safe_emit(
                "analyzing", f"v2_debate_budget_skip_{ctx.ticker}",
                f"{ctx.ticker}: Debate SKIPPED — only {_remaining:.0f}s left "
                f"(need {_THESIS_BUDGET_SECONDS + 300}s)",
                status="warning",
            )

    if skip_debate and not ctx.orchestrator_had_agents:
        logger.info(
            "[V2] Skipping adversarial debate for %s — MetaOrchestrator produced 0 real agents",
            ctx.ticker,
        )
        ctx.safe_emit(
            "analyzing", f"v2_debate_skip_{ctx.ticker}",
            f"{ctx.ticker}: Debate SKIPPED — MetaOrchestrator had 0 agents (saving GPU for thesis)",
            status="warning",
        )
        return ctx

    if skip_debate:
        return ctx

    # ── Run debate ──
    try:
        from app.cognition.debate.debate_coordinator import run_adversarial_debate

        ctx.safe_emit(
            "analyzing", f"v2_debate_{ctx.ticker}",
            f"{ctx.ticker}: Starting adversarial debate "
            f"({'HOLD-vs-SELL' if ctx.held else 'BUY-vs-SELL'})...",
            status="running",
        )
        await cycle_control.wait_if_paused()
        t_debate = time.monotonic()

        ctx.debate_result = await asyncio.wait_for(
            run_adversarial_debate(
                ticker=ctx.ticker,
                packet=ctx.packet,
                cycle_id=ctx.cycle_id,
                bot_id=ctx.bot_id,
                agent_insights=ctx.agent_insights,
                position_context=ctx.position_context,
                portfolio_dashboard=ctx.portfolio_dashboard,
                ctx=ctx,
            ),
            timeout=300.0
        )
        ms_debate = ctx.elapsed_ms(t_debate)

        if ctx.debate_result:
            ctx.add_tokens(ctx.debate_result.total_tokens)
            ctx.add_stage("adversarial_debate", ms_debate)

            emoji_d = (
                "🟢" if ctx.debate_result.judge_action == "BUY"
                else "🔴" if ctx.debate_result.judge_action == "SELL"
                else "🟡"
            )
            ctx.safe_emit(
                "analyzing", f"v2_debate_done_{ctx.ticker}",
                f"{emoji_d} {ctx.ticker}: Debate verdict — {ctx.debate_result.judge_action} @ "
                f"{ctx.debate_result.judge_confidence}% (winner: {ctx.debate_result.winning_side}, "
                f"integrity: {ctx.debate_result.integrity_status})",
                elapsed_ms=ms_debate,
            )
            log_manager.log_v2_cycle(ctx.cycle_id, "v2_debate_result", {
                "ticker": ctx.ticker,
                "action": ctx.debate_result.judge_action,
                "confidence": ctx.debate_result.judge_confidence,
                "winner": ctx.debate_result.winning_side,
                "integrity": ctx.debate_result.integrity_status,
                "bull_claims": len(ctx.debate_result.bull_claims),
                "bear_claims": len(ctx.debate_result.bear_claims),
                "verified_bull": len(ctx.debate_result.verified_bull_claims),
                "verified_bear": len(ctx.debate_result.verified_bear_claims),
                "unverified": len(ctx.debate_result.unverified_claims),
                "rationale": ctx.debate_result.judge_rationale[:500] if ctx.debate_result.judge_rationale else "",
                "key_factor": ctx.debate_result.key_deciding_factor or "",
                "persona_outcomes": ctx.debate_result.persona_outcomes or {},
                "total_debate_tokens": ctx.debate_result.total_tokens,
                "elapsed_ms": ms_debate,
            })

            # ── Emit Agent Voice Quote for Debate ──
            try:
                from app.services.agent_voice_service import dispatch_agent_quote
                winner = ctx.debate_result.winning_side.lower()  # "bull" or "bear"
                agent_id = "BULLISH_DEBATER" if winner == "bull" else "BEARISH_DEBATER"
                archetype = "BULL" if winner == "bull" else "BEAR"
                dispatch_agent_quote(
                    agent_id=agent_id,
                    archetype=archetype,
                    context={
                        "ticker": ctx.ticker,
                        "cycle_id": ctx.cycle_id,
                        "tool": "adversarial_debate",
                        "action_result": ctx.debate_result.judge_action,
                        "agent_insight": ctx.debate_result.transcript,
                    }
                )
            except Exception as voice_err:
                logger.debug("Voice event trigger failed: %s", voice_err)
        else:
            ctx.safe_emit(
                "analyzing", f"v2_debate_skip_{ctx.ticker}",
                f"{ctx.ticker}: Debate skipped (disabled or no analyst endpoints)",
                status="warning",
            )

    except Exception as e:
        logger.warning("[V2] Adversarial debate failed for %s (non-fatal): %s", ctx.ticker, e)
        ctx.safe_emit(
            "analyzing", f"v2_debate_fail_{ctx.ticker}",
            f"{ctx.ticker}: Debate failed — {e}",
            status="warning",
        )

    return ctx
