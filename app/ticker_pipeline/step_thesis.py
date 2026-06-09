"""
Step: Thesis Generation.

Builds the extra context (portfolio, position, constitution, debate, macro,
ontology, memory, agent insights, autoresearch lessons), then calls
generate_thesis() with retry logic.

Extracted from runner.py Step 6.
"""

import asyncio
import logging
import time

from app.ticker_pipeline.context import TickerContext
from app.log_manager import log_manager

logger = logging.getLogger(__name__)

# Cap extra context to ~2000 tokens (4 chars/token)
MAX_EXTRA_CONTEXT_CHARS = 8000


async def run_thesis_step(ctx: TickerContext) -> TickerContext:
    """Generate thesis via LLM with retry logic."""
    from app.agents.debate_agents.thesis_agent import generate_thesis
    from app.cycle.orchestration.cycle_control import cycle_control

    # ── Build extra context ──
    extra_context = _build_extra_context(ctx)

    ctx.safe_emit(
        "analyzing", f"v2_thesis_{ctx.ticker}",
        f"{ctx.ticker}: Generating thesis via LLM...",
        status="running",
    )

    await cycle_control.wait_if_paused()
    t6 = time.monotonic()

    # ── Guarded execution with semaphore ──
    async def _run_with_retry():
        kwargs = dict(
            entity_id=ctx.ticker,
            packet=ctx.packet,
            bias="neutral",
            cycle_id=ctx.cycle_id,
            bot_id=ctx.bot_id,
            extra_context=extra_context,
            watchlist=ctx.watchlist or [],
            held=ctx.held,
        )

        _thesis_timeouts = [180.0, 120.0]
        _max_attempts = len(_thesis_timeouts)

        for attempt in range(_max_attempts):
            timeout = _thesis_timeouts[attempt]
            try:
                thesis, thesis_tokens = await asyncio.wait_for(
                    generate_thesis(**kwargs), timeout=timeout,
                )

                # Check for malformed/empty JSON
                if thesis.confidence == 0 and not thesis.core_claims:
                    raise ValueError(
                        f"LLM returned malformed JSON or EMPTY_SIGNAL: {thesis.rationale}"
                    )

                if attempt > 0:
                    logger.info(
                        "[V2] Thesis generation SUCCEEDED for %s on retry %d/%d",
                        ctx.ticker, attempt + 1, _max_attempts,
                    )
                return thesis, thesis_tokens

            except (asyncio.TimeoutError, ValueError) as err:
                is_timeout = isinstance(err, asyncio.TimeoutError)
                err_msg = "TIMEOUT" if is_timeout else "PARSE_ERROR"

                if attempt < _max_attempts - 1:
                    logger.warning(
                        "[V2] Thesis %s for %s (attempt %d/%d, %.0fs) — waiting 30s before retry",
                        err_msg, ctx.ticker, attempt + 1, _max_attempts, timeout,
                    )
                    log_manager.log_cycle_error(
                        ctx.cycle_id, f"thesis_{err_msg.lower()}_retry",
                        ticker=ctx.ticker, error=str(err), stage="thesis_generation",
                        elapsed_ms=ctx.elapsed_ms(t6),
                        extra={"attempt": attempt + 1, "max_attempts": _max_attempts, "timeout_s": timeout},
                    )
                    ctx.safe_emit(
                        "analyzing", f"v2_thesis_retry_{ctx.ticker}",
                        f"{ctx.ticker}: Thesis {err_msg} (attempt {attempt + 1}/{_max_attempts}) — retrying after 30s",
                        status="warning",
                    )
                    await asyncio.sleep(30)
                else:
                    logger.error(
                        "[V2] Thesis generation %s for %s after %d attempts",
                        err_msg, ctx.ticker, _max_attempts,
                    )
                    ctx.safe_emit(
                        "analyzing", f"v2_thesis_timeout_{ctx.ticker}",
                        f"{ctx.ticker}: Thesis LLM {err_msg} (all {_max_attempts} attempts exhausted)",
                        status="error",
                    )
                    log_manager.log_v2_cycle(ctx.cycle_id, "v2_error", {
                        "ticker": ctx.ticker,
                        "error": f"Thesis generation timed out after {_max_attempts} attempts",
                        "error_type": "TimeoutError", "stages_completed": ctx.stages,
                        "elapsed_ms": ctx.elapsed_ms(),
                        "attempts": _max_attempts,
                    })
                    raise RuntimeError(
                        f"Thesis generation timed out after {_max_attempts} attempts"
                    ) from None

    # Wait for the semaphore slot (if configured) before running the retry loop
    if ctx.thesis_semaphore:
        async with ctx.thesis_semaphore:
            ctx.thesis, ctx.thesis_tokens = await _run_with_retry()
    else:
        ctx.thesis, ctx.thesis_tokens = await _run_with_retry()

    ctx.add_tokens(ctx.thesis_tokens)
    ms6 = ctx.elapsed_ms(t6)
    ctx.add_stage("thesis_generation", ms6)
    log_manager.log_v2_cycle(ctx.cycle_id, "v2_thesis", {
        "ticker": ctx.ticker, "action": ctx.thesis.action,
        "confidence": ctx.thesis.confidence,
        "claims_count": len(ctx.thesis.core_claims),
        "weaknesses_count": len(ctx.thesis.weaknesses),
        "tokens": ctx.thesis_tokens, "elapsed_ms": ms6,
    })

    # ── Meta-auditor (non-blocking) ──
    try:
        from app.services.logging.meta_auditor import audit_thesis_quality
        audit_task = asyncio.create_task(
            audit_thesis_quality(
                ctx.ticker, str(ctx.packet.structured_facts),
                ctx.thesis.rationale, ctx.cycle_id, ctx.bot_id,
            )
        )
        audit_result = await asyncio.wait_for(audit_task, timeout=15.0)
        log_manager.log_v2_cycle(ctx.cycle_id, "meta_audit", {"ticker": ctx.ticker, "audit": audit_result})
    except asyncio.TimeoutError:
        logger.warning("[V2] Meta-auditor timed out for %s", ctx.ticker)
    except Exception as e:
        logger.warning("[V2] Meta-auditor failed for %s: %s", ctx.ticker, e)

    ctx.safe_emit(
        "analyzing", f"v2_thesis_done_{ctx.ticker}",
        f"{ctx.ticker}: Thesis → {ctx.thesis.action} @ {ctx.thesis.confidence}% "
        f"(claims: {len(ctx.thesis.core_claims)}, weaknesses: {len(ctx.thesis.weaknesses)})",
        elapsed_ms=ms6,
    )

    # ── Set final decision from thesis ──
    _set_final_decision(ctx)

    # ── Emit Agent Voice Quote for Thesis ──
    try:
        from app.services.agent_voice_service import dispatch_agent_quote
        dispatch_agent_quote(
            agent_id="QUANT_RESEARCH_AGENT",
            archetype="QUANT",
            context={
                "ticker": ctx.ticker,
                "tool": "thesis_generation",
                "action_result": ctx.final_action,
            }
        )
    except Exception as voice_err:
        logger.debug("Voice event trigger failed: %s", voice_err)

    return ctx


def _build_extra_context(ctx: TickerContext) -> str:
    """Assemble the extra context string from all available sources."""
    parts: list[str] = []
    budget_used = 0

    # Portfolio risk dashboard (highest priority)
    if ctx.portfolio_dashboard:
        parts.append(ctx.portfolio_dashboard)
        budget_used += len(ctx.portfolio_dashboard)

    # Position context
    if ctx.position_context.get("held"):
        try:
            from app.tools.portfolio_tools import format_position_context_for_prompt
            pos_block = format_position_context_for_prompt(ctx.position_context)
            if pos_block:
                parts.append(pos_block)
                budget_used += len(pos_block)
        except Exception:
            pass

    # Ontology / Brain Graph context (high priority — this is the system's
    # learned intelligence from past cycles, claims, and entity extraction).
    # Guaranteed a minimum of 1500 chars before anything else can eat the budget.
    ontology_text = ctx.ontology_ctx.get("ontology_context", "")
    if ontology_text:
        ontology_budget = max(1500, MAX_EXTRA_CONTEXT_CHARS - budget_used - 4000)
        trimmed_onto = ontology_text[:ontology_budget]
        parts.append(trimmed_onto)
        budget_used += len(trimmed_onto)

    # Trading Constitution
    try:
        from app.pipeline.trading_constitution import format_constitution_for_prompt
        constitution_block = format_constitution_for_prompt()
        if constitution_block:
            remaining = MAX_EXTRA_CONTEXT_CHARS - budget_used
            if remaining > 200:
                trimmed = constitution_block[:remaining]
                parts.append(trimmed)
                budget_used += len(trimmed)
    except Exception:
        pass

    # Debate result
    if ctx.debate_result and ctx.debate_result.judge_rationale:
        debate_summary = (
            f"# ADVERSARIAL DEBATE RESULT\n"
            f"**Verdict:** {ctx.debate_result.judge_action} @ {ctx.debate_result.judge_confidence}%\n"
            f"**Winner:** {ctx.debate_result.winning_side}\n"
            f"**Key Factor:** {ctx.debate_result.key_deciding_factor}\n"
            f"**Rationale:** {ctx.debate_result.judge_rationale}\n"
            f"**Evidence Quality:** {ctx.debate_result.integrity_status} "
            f"({len(ctx.debate_result.unverified_claims)} claims rejected)\n"
            f"**Bull claims verified:** {len(ctx.debate_result.verified_bull_claims)}/{len(ctx.debate_result.bull_claims)}\n"
            f"**Bear claims verified:** {len(ctx.debate_result.verified_bear_claims)}/{len(ctx.debate_result.bear_claims)}"
        )
        parts.append(debate_summary)
        budget_used += len(debate_summary)

    # Macro memo
    if ctx.macro_memo:
        part = f"# MACRO STRATEGY MEMO\n{ctx.macro_memo}"
        parts.append(part)
        budget_used += len(part)

    # Memory
    mem_brief = ctx.memory_context.get("memory_brief", "")
    if mem_brief and mem_brief != "No prior memory.":
        remaining = MAX_EXTRA_CONTEXT_CHARS - budget_used
        if remaining > 100:
            trimmed = mem_brief[:min(500, remaining)]
            parts.append(f"# PRIOR MEMORY\n{trimmed}")
            budget_used += len(trimmed) + 16

    # Agent insights
    if ctx.agent_insights:
        remaining = MAX_EXTRA_CONTEXT_CHARS - budget_used
        if remaining > 200:
            insights_str = "\n".join(
                f"## {k.upper()} AGENT INSIGHT\n{v}"
                for k, v in ctx.agent_insights.items()
            )
            part = f"# SPECIALIZED AGENT INSIGHTS\n{insights_str}"
            parts.append(part[:remaining])
            budget_used += len(part[:remaining])

    # Autoresearch lessons
    try:
        from app.cognition.lesson_store import retrieve_lessons
        lessons = retrieve_lessons(ctx.ticker, k=2)
        if lessons:
            lesson_texts = "\n".join(f"- {l.get('lesson_text', '')}" for l in lessons)
            part = (
                "# AUTORESEARCH LESSONS\n"
                "The following are critical lessons and recommendations from past autoresearch cycles. "
                "You MUST adhere to these rules to avoid repeating past mistakes:\n\n"
                f"{lesson_texts}"
            )
            remaining = MAX_EXTRA_CONTEXT_CHARS - budget_used
            if remaining > 200:
                parts.append(part[:remaining])
                budget_used += len(part[:remaining])
            ctx.safe_emit(
                "analyzing", f"autoresearch_{ctx.ticker}",
                f"{ctx.ticker}: Injected {len(lessons)} Autoresearch lessons",
            )
    except Exception as ar_err:
        logger.warning("[V2] Failed to retrieve autoresearch lessons: %s", ar_err)

    return "\n\n".join(parts)


def _set_final_decision(ctx: TickerContext) -> None:
    """Set final_action/confidence/rationale from thesis, handling EMPTY_SIGNAL."""
    if ctx.thesis.confidence == 0 and not ctx.thesis.core_claims:
        logger.warning(
            "[V2] ⚠️ EMPTY_SIGNAL for %s: action=%s confidence=0 claims=0 tokens=%d",
            ctx.ticker, ctx.thesis.action, ctx.thesis_tokens,
        )
        log_manager.log_v2_cycle(ctx.cycle_id, "v2_empty_signal", {
            "ticker": ctx.ticker, "original_action": ctx.thesis.action,
            "tokens": ctx.thesis_tokens, "reason": "confidence_0_no_claims",
        })
        from app.cognition.debate.action_gate import gate_action
        ctx.final_action = gate_action("HOLD", ctx.held)
        ctx.final_confidence = 0
        ctx.final_rationale = (
            f"⚠️ PIPELINE FAILURE (EMPTY_SIGNAL): Thesis returned confidence=0 with 0 claims. "
            f"Original action was {ctx.thesis.action}. This is NOT a valid trading signal — "
            f"the analysis pipeline failed to produce meaningful output. "
            f"Defaulting to {ctx.final_action} to prevent erroneous trades."
        )
        ctx.safe_emit(
            "analyzing", f"v2_empty_signal_{ctx.ticker}",
            f"⚠️ {ctx.ticker}: EMPTY_SIGNAL detected — forced {ctx.final_action} (pipeline failure)",
            status="error",
        )
        ctx.failure_diagnosis = {
            "failure_type": "EMPTY_SIGNAL",
            "stages_completed": list(ctx.stages),
            "meta_orchestrator_agents": len(ctx.agent_insights),
            "tokens_per_stage": dict(ctx.stage_timings),
            "error_chain": [
                f"Thesis returned confidence=0 with 0 claims",
                f"Original thesis action was {ctx.thesis.action}",
                f"Total tokens consumed: {ctx.thesis_tokens}",
                f"MetaOrchestrator had agents: {ctx.orchestrator_had_agents}",
            ],
            "data_available": (
                f"{len(ctx.packet.structured_facts)} structured facts, "
                f"{len(ctx.packet.claims)} claims, "
                f"missing: {ctx.packet.missing_fields}"
            ),
        }
    else:
        ctx.final_action = ctx.thesis.action
        ctx.final_confidence = ctx.thesis.confidence
        ctx.final_rationale = ctx.thesis.rationale
