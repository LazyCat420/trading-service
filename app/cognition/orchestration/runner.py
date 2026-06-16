"""
Cognition V2 — Pipeline Runner.

Executes the full V2 cognition sequence for a single ticker:
  1. Ontology enrichment (optional, non-blocking)
  2. Evidence Packet build from DB
  3. Sufficiency Gate check
  4. Dynamic retrieval loop (max 1 retry on critical gaps)
  5. Memory context injection (prior episodes + procedural rules)
  6. Thesis generation via LLM
  7. Episodic memory write-back
  8. V2 cycle log

Returns a dict matching V1's analyze_ticker() shape so downstream
phases (trading, post-cycle hooks, reports) work unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from app.cognition.orchestration.models import CognitionRunResult
from app.log_manager import log_manager

logger = logging.getLogger(__name__)

# ── Chart task registry ──────────────────────────────────────────────
# Chart generation tasks are spawned per-ticker and run in the background.
# This registry lets the orchestrator cancel them cleanly at cycle end,
# preventing the zombie-freeze bug where pause() traps orphan tasks.
_chart_tasks: set[asyncio.Task] = set()


def drain_chart_tasks() -> list[asyncio.Task]:
    """Return and clear all tracked chart tasks for cancellation."""
    tasks = list(_chart_tasks)
    _chart_tasks.clear()
    return tasks


from app.services.logging.tracer import trace_span

@trace_span("orchestration.execute_v2_pipeline")
async def execute_v2_pipeline(
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
) -> dict[str, Any]:
    """Run the full V2 cognition pipeline for a single ticker.

    Delegates to app.ticker_pipeline.pipeline.execute_ticker_pipeline()
    which breaks the pipeline into composable Lego steps.

    Returns a dict with the same keys as V1's analyze_ticker() so the
    trading phase, post-cycle hooks, and report generation work unchanged.
    """
    from app.ticker_pipeline.pipeline import execute_ticker_pipeline

    return await execute_ticker_pipeline(
        ticker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        emit=emit,
        macro_memo=macro_memo,
        watchlist=watchlist,
        db_semaphore=db_semaphore,
        thesis_semaphore=thesis_semaphore,
        is_highly_redundant=is_highly_redundant,
        research_focus=research_focus,
        trigger_type=trigger_type,
    )


# ── Legacy code below: kept for backward compat ──────────────────────
# _build_v1_compatible_result is still used by execute_open_position_fast_track.
# The new ticker_pipeline uses app.core.result_builder instead.



def _build_v1_compatible_result(
    *,
    ticker: str,
    action: str,
    confidence: int | float,
    rationale: str,
    cycle_id: str,
    total_tokens: int,
    elapsed: float,
    stages: list[str],
    config_used: str,
    thesis: Any = None,
    sufficiency: Any = None,
    memory_context: dict[str, Any] | None = None,
    debate_result: Any = None,
    agent_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a result dict matching V1's analyze_ticker() output shape.

    This ensures trading_phase, post_cycle_hooks, report_service, and
    the frontend all work without modification.
    """
    v2_meta: dict[str, Any] = {
        "stages_completed": stages,
        "sufficiency_status": sufficiency.status if sufficiency else None,
        "thesis_action": thesis.action if thesis else None,
        "thesis_confidence": thesis.confidence if thesis else None,
        "thesis_weaknesses": thesis.weaknesses if thesis else [],
        "memory_episodes": (
            memory_context.get("episode_count", 0) if memory_context else 0
        ),
        "memory_rules": (memory_context.get("rule_count", 0) if memory_context else 0),
    }

    # Include debate metadata if available
    if debate_result:
        v2_meta["debate"] = {
            "judge_action": debate_result.judge_action,
            "judge_confidence": debate_result.judge_confidence,
            "winning_side": debate_result.winning_side,
            "integrity_status": debate_result.integrity_status,
            "bull_claims_verified": f"{len(debate_result.verified_bull_claims)}/{len(debate_result.bull_claims)}",
            "bear_claims_verified": f"{len(debate_result.verified_bear_claims)}/{len(debate_result.bear_claims)}",
            "unverified_claims": len(debate_result.unverified_claims),
            "key_deciding_factor": debate_result.key_deciding_factor,
            "transcript": debate_result.transcript,
            "total_tokens": debate_result.total_tokens,
            "original_thesis_status": getattr(debate_result, "original_thesis_status", "NOT_HELD"),
            "original_thesis_explanation": getattr(debate_result, "original_thesis_explanation", ""),
        }

    return {
        "ticker": ticker,
        "action": action,
        "confidence": int(confidence),
        "rationale": rationale,
        "config_used": config_used,
        "triage_tier": sufficiency.status if sufficiency else "standard",
        "escalated": debate_result is not None,
        "agent_results": agent_results or {},
        "c_result": {
            "action": action,
            "confidence": int(confidence),
            "rationale": rationale,
        },
        "d_result": {
            "action": debate_result.judge_action,
            "confidence": debate_result.judge_confidence,
            "original_thesis_status": getattr(debate_result, "original_thesis_status", "NOT_HELD"),
            "original_thesis_explanation": getattr(debate_result, "original_thesis_explanation", ""),
        }
        if debate_result
        else None,
        "human_review": False,
        "agent_tokens": 0,
        "rlm_tokens": total_tokens,
        "total_tokens": total_tokens,
        "total_time_s": round(elapsed, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # V2-specific metadata (ignored by V1 consumers, useful for debugging)
        "v2_metadata": v2_meta,
    }


# ── Position Monitor Agent Fast-Track Prompt & Function ───────────────

POSITION_MONITOR_SYSTEM_PROMPT = """You are the Position Monitor Agent. Your task is to evaluate whether we should HOLD or SELL (exit/trim) our existing position in {ticker}.

## POSITION STATE:
- Ticker: {ticker}
- Average Entry Price: ${avg_entry}
- Current Price: ${current_price}
- Unrealized P&L: {unrealized_pnl_pct:+.2f}%
- Days Held: {holding_days} days
- Sector: {sector}

## PORTFOLIO RISK & CORRELATION DASHBOARD:
{portfolio_dashboard}

## MACRO ENVIRONMENT MEMO:
{macro_memo}

## EVIDENCE PACKET DATA (Recent News & Technicals):
{evidence_summary}

## DECISION INSTRUCTIONS:
- You MUST output a decision of either HOLD or SELL.
- Set the confidence score from 0 to 100 based on the strength of the evidence.
- Provide a clear, 2-3 sentence rationale explaining the decision.
- Focus on risk management: check stop-loss/take-profit targets, momentum, and whether there is any critical news.

## OUTPUT FORMAT:
You MUST respond with a valid JSON object matching this schema:
{{
    "action": "HOLD|SELL",
    "confidence": 85,
    "rationale": "Your 2-3 sentence rationale here."
}}
"""


async def execute_open_position_fast_track(
    ticker: str,
    *,
    cycle_id: str = "",
    bot_id: str = "",
    emit: Callable[..., Any] | None = None,
    macro_memo: str = "",
    position_context: dict = None,
    portfolio_dashboard: str = "",
    thesis_semaphore: asyncio.Semaphore | None = None,
    db_semaphore: asyncio.Semaphore | None = None,
    start_time: float = 0.0,
) -> dict[str, Any] | None:
    """Run a fast-track single-agent evaluation for a held portfolio position.

    Bypasses the multi-agent specialist routing, debate, and thesis steps
    to finish in seconds while remaining context-aware.
    """
    from app.utils.pipeline_utils import noop as _noop
    from app.utils.pipeline_utils import elapsed_ms
    from app.cognition.evidence.packet_builder import build_evidence_packet

    if emit is None:
        emit = _noop

    emit(
        "analyzing",
        f"v2_fast_track_start_{ticker}",
        f"⚡ {ticker}: Open Position Fast-Track Monitor starting",
        status="running",
    )

    # 1. Build Evidence Packet (Pure DB retrieval, very fast)
    t_ev = time.monotonic()
    packet = await build_evidence_packet(ticker)
    ms_ev = elapsed_ms(t_ev)

    # Extract clean evidence summary
    facts_str = "\n".join(f"- {f.field_name if hasattr(f, 'field_name') else str(f)}: {f.value if hasattr(f, 'value') else ''}" for f in packet.structured_facts)
    claims_str = "\n".join(f"- {c.text if hasattr(c, 'text') else str(c)} (source: {c.source_url if hasattr(c, 'source_url') else ''})" for c in packet.claims)
    evidence_summary = (
        f"### Technicals & Fundamentals:\n{facts_str or 'No structured facts available.'}\n\n"
        f"### Recent Qualitative Claims:\n{claims_str or 'No claims available.'}"
    )

    # 2. Format System Prompt
    avg_entry = position_context.get("avg_entry", 0.0)
    current_price = position_context.get("current_price", 0.0)
    unrealized_pnl_pct = position_context.get("unrealized_pnl_pct", 0.0)
    holding_days = position_context.get("holding_days", 0)
    sector = position_context.get("sector", "default")

    system_prompt = POSITION_MONITOR_SYSTEM_PROMPT.format(
        ticker=ticker,
        avg_entry=avg_entry,
        current_price=current_price,
        unrealized_pnl_pct=unrealized_pnl_pct,
        holding_days=holding_days,
        sector=sector,
        portfolio_dashboard=portfolio_dashboard or "Not available.",
        macro_memo=macro_memo or "No macro memo available.",
        evidence_summary=evidence_summary,
    )

    user_prompt = (
        f"Evaluate the position in {ticker}.\n"
        f"Determine whether we should HOLD or SELL.\n"
        f"Output JSON response matching the requested schema."
    )

    # 3. Call Position Monitor Agent (Lightweight, single LLM call)
    from app.agents.base_agent import run_agent
    from app.utils.text_utils import parse_json_response

    if thesis_semaphore:
        async with thesis_semaphore:
            agent_result = await run_agent(
                agent_name="position_monitor",
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id=bot_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=8192,
                enable_tools=False,
            )
    else:
        agent_result = await run_agent(
            agent_name="position_monitor",
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=8192,
            enable_tools=False,
        )

    response_text = agent_result.get("response", "")
    tokens_used = agent_result.get("tokens_used", 0)

    # 4. Parse response
    parsed_json = {}
    try:
        parsed_json = parse_json_response(response_text)
    except Exception as parse_err:
        logger.warning(
            "[V2] [Fast-Track] parse_json_response failed for %s: %s",
            ticker, parse_err,
        )

    action = parsed_json.get("action", "HOLD").upper()
    confidence = parsed_json.get("confidence", 80)
    rationale = parsed_json.get("rationale", "Fast-track position monitor executed.")

    # 5. Form V1-compatible result
    class DummySufficiency:
        status = "sufficient"
        warnings = []

    elapsed = time.monotonic() - start_time
    result = _build_v1_compatible_result(
        ticker=ticker,
        action=action,
        confidence=confidence,
        rationale=rationale,
        cycle_id=cycle_id,
        total_tokens=tokens_used,
        elapsed=elapsed,
        stages=["fast_track_evidence", "fast_track_monitor"],
        config_used="v2_position_fast_track",
        sufficiency=DummySufficiency(),
        agent_results={
            "position_monitor": {
                "response": response_text,
                "tokens": tokens_used,
            }
        }
    )

    # Attach transient report data for post phase
    try:
        _structured_facts = []
        for fact in packet.structured_facts[:100]:
            _structured_facts.append({
                "field_name": getattr(fact, "field_name", str(fact)),
                "value": getattr(fact, "value", None),
                "source": getattr(fact, "source", "unknown"),
            })
    except Exception:
        _structured_facts = []

    result["_report_data"] = {
        "agent_insights": {"position_monitor": rationale},
        "debate_result": None,
        "thesis": None,
        "sufficiency": DummySufficiency(),
        "stages": ["fast_track_evidence", "fast_track_monitor"],
        "stage_timings": {
            "fast_track_evidence": int(ms_ev),
            "fast_track_monitor": int(elapsed * 1000 - ms_ev)
        },
        "hallucination_result": None,
        "memory_brief": "",
        "present_sources": [],
        "missing_sources": [],
        "freshness_summary": getattr(packet, "freshness_summary", None),
        "structured_facts": _structured_facts,
        "failure_diagnosis": None,
    }

    # DB log (analysis_results table)
    try:
        from app.pipeline.analysis.decision_engine import _log_decision
        if db_semaphore:
            async with db_semaphore:
                _log_decision(result, cycle_id, bot_id)
        else:
            _log_decision(result, cycle_id, bot_id)
    except Exception as e:
        logger.warning("[V2] [Fast-Track] _log_decision failed for %s: %s", ticker, e)

    # Post-cycle hooks
    try:
        from app.cycle.orchestration.post_cycle_hooks import run_post_cycle_hooks
        await run_post_cycle_hooks(
            ticker=ticker,
            result=result,
            escalated=False,
            cycle_id=cycle_id,
            final_action=action,
            final_confidence=confidence,
        )
    except Exception as hooks_err:
        logger.warning("[V2] [Fast-Track] Post-cycle hooks failed for %s: %s", ticker, hooks_err)

    # Attention record
    try:
        from app.cycle.attention_tracker import record_analysis as _record_attn
        _record_attn(
            ticker,
            action=action,
            confidence=confidence,
            was_deep=False,
        )
    except Exception as attn_err:
        logger.warning("[V2] [Fast-Track] Attention tracker failed for %s: %s", ticker, attn_err)

    emit(
        "analyzing",
        f"v2_done_{ticker}",
        f"✅ {ticker}: Fast-Track verdict → {action} @ {confidence}% in {elapsed:.1f}s",
        status="ok",
        data={"action": action, "confidence": confidence, "elapsed_ms": int(elapsed * 1000)},
    )

    log_manager.log_v2_cycle(
        cycle_id,
        "v2_pipeline_complete",
        {
            "ticker": ticker,
            "action": action,
            "confidence": confidence,
            "elapsed_ms": int(elapsed * 1000),
            "total_tokens": tokens_used,
            "stages_completed": result["_report_data"]["stages"],
            "config_used": "v2_position_fast_track",
        }
    )

    return result


async def execute_v2_tickers(
    tickers: list[str],
    *,
    cycle_id: str = "",
    bot_id: str = "",
    emit: Callable[..., Any] | None = None,
    macro_memo: str = "",
) -> list[dict[str, Any]]:
    """Run V2 pipeline for multiple tickers. Throttled parallel via semaphore.

    Matches the signature of V1's analyze_tickers() so it can be a
    drop-in replacement.

    Concurrency model:
      - Ticker parallelism: completely parallel (unbounded) to maximize throughput
      - LLM dispatch: gated by per-endpoint PriorityQueues in vllm_client.py
      - DB writes: serialized by a separate semaphore (8 concurrent max)
        to avoid TransactionException on the shared connection.
    """
    import asyncio
    from app.utils.pipeline_utils import noop as _noop
    from app.config import settings

    if emit is None:
        emit = _noop

    # Load memory snapshot once for the cycle (same pattern as V1)
    try:
        from app.cognition.trading_memory import trading_memory

        trading_memory.load_from_disk()
    except Exception as mem_err:
        logger.warning("[V2] Memory load failed (non-fatal): %s", mem_err)

    timeout_seconds = settings.CYCLE_TIMEOUT_MINUTES * 60

    # LLM analysis concurrency is actively handled by vllm_client's PriorityQueues.
    # Therefore, no global pipeline throttling semaphore is needed here.

    # DB write serialization — prevents concurrent connection pooling issues.
    # Only protects _log_decision() and write_episode(), NOT the full pipeline.
    db_semaphore = asyncio.Semaphore(8)

    async def _run_ticker(t: str) -> dict[str, Any]:
        return await execute_v2_pipeline(
            t,
            cycle_id=cycle_id,
            bot_id=bot_id,
            emit=emit,
            macro_memo=macro_memo,
            watchlist=tickers,
            db_semaphore=db_semaphore,
        )

    logger.info(
        "[V2] Launching parallel analysis for %d tickers (vLLM queues handle dispatch limits)",
        len(tickers),
    )
    emit(
        "analyzing",
        "v2_all_tickers",
        f"V2: Launching parallel analysis for {len(tickers)} tickers",
        status="running",
    )

    try:
        raw_results = await asyncio.wait_for(
            asyncio.gather(
                *[_run_ticker(t) for t in tickers],
                return_exceptions=True,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[V2] CYCLE TIMEOUT after %d min",
            settings.CYCLE_TIMEOUT_MINUTES,
        )
        emit(
            "analyzing",
            "v2_timeout",
            f"V2 cycle timeout ({settings.CYCLE_TIMEOUT_MINUTES}min)",
            status="error",
        )
        return [{"ticker": t, "error": "cycle_timeout"} for t in tickers]

    results = []
    for t, r in zip(tickers, raw_results):
        if isinstance(r, Exception):
            logger.error("[V2] %s failed: %s", t, r)
            import traceback

            try:
                from app.cycle.orchestration.state_manager import PipelineStateDB

                PipelineStateDB.log_execution_error(
                    cycle_id or "unknown",
                    "cognition_runner",
                    t,
                    type(r).__name__,
                    str(r),
                    "".join(traceback.format_exception(type(r), r, r.__traceback__)),
                )
            except Exception:
                pass
            results.append({"ticker": t, "error": str(r)})
            emit("analyzing", f"v2_error_{t}", f"{t}: V2 FAILED — {r}", status="error")
        else:
            results.append(r)

    return results
