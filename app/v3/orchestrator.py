"""
V3 Orchestrator — The 4-Layer Linear Pipeline traffic controller.

Advances a ticker through: Context Init → Research → Debate → Decision.
Never inspects data or makes trading decisions — strictly a state machine + scheduler.

Activated when PIPELINE_VERSION=v3 is set in the environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from app.v3.shared_desk import (
    SharedDesk, DeskPhase, PhaseOutcome, DecisionProvenance,
    tournament_debate_mode, TOURNAMENT_MODE_SHADOW,
)
from app.v3.guardrails import CircuitBreaker, research_degraded
from app.services.adaptive_concurrency import concurrency_controller
from app.v3.telemetry import persist_telemetry
from app.v3.agent_runner import run_v3_agent
from app.v3.desk_persistence import save_desk

logger = logging.getLogger(__name__)

# Fire-and-forget background tasks (e.g. memory consolidation) — a bare
# create_task result gets garbage-collected mid-flight without this anchor.
_BG_TASKS: set = set()


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

    agent_locale: str = "default",
    prism_overrides: dict | None = None,
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
        data_report = await build_ticker_data_report(ticker, emit=emit, cycle_id=cycle_id)
        emit(
            "analyzing", f"v3_precollect_ok_{ticker}",
            f"📥 {ticker}: Market & news pre-collection complete",
            status="ok",
        )
    except Exception as e:
        logger.error("[V3] Failed to pre-collect data for %s: %s", ticker, e)
        data_report = f"Failed to pre-collect stock data: {e}"
        # Report-assembly failures must show up in the cycle summary — the
        # individual collectors may all have succeeded, but the ticker is
        # still analyzed data-blind, so count it as a collector failure.
        try:
            from app.v3.collector_stats import record as _record_cstats
            _record_cstats(cycle_id, ticker, ok=[], errored=["data_report"],
                           timed_out=[], skipped=[])
        except Exception:
            pass
        emit(
            "analyzing", f"v3_precollect_err_{ticker}",
            f"📥 {ticker}: data report FAILED — agents run without pre-collected data: {e}",
            status="error",
        )

    # Inject cycle metadata
    desk.cycle_metadata = _build_cycle_metadata(
        ticker=ticker,
        bot_id=bot_id,
        macro_memo=macro_memo,
        research_focus=research_focus,
        trigger_type=trigger_type,
    )

    desk.cycle_metadata["agent_locale"] = agent_locale
    desk.cycle_metadata["prism_overrides"] = prism_overrides or {}
    
    # Store the pre-collected report
    desk.cycle_metadata["data_report"] = data_report

    # Live macro snapshot for the Regime Engine. It classifies the GLOBAL
    # market state but the per-ticker data_report gives it nothing macro, so
    # it was producing a regime from thin air (1 turn, no tools, lowest
    # quality). Inject real VIX/index/yield/dollar levels so the classification
    # is grounded. Non-fatal — the engine still has its tools as a fallback.
    try:
        from app.collectors.market_regime_collector import get_latest_market_snapshot
        macro_briefing = _format_macro_briefing(get_latest_market_snapshot())
        if macro_briefing:
            desk.cycle_metadata["macro_briefing"] = macro_briefing
    except Exception as e:
        logger.warning("[V3] %s: macro snapshot unavailable (non-fatal): %s", ticker, e)

    # Precomputed quant math (2026-07-21 research audit): the quant analyst
    # averages 1.6 loops and the board 1.0 — prompts telling them to CALL the
    # GARCH/HRP tools mostly never fire. Compute the math in code here and
    # inject the results into their prompts (agent_runner scopes the block to
    # quant + board). Off-loop via to_thread: GARCH is ~1s of CPU + DB reads.
    try:
        from app.quant.context_block import build_quant_math_block
        # 60s, raised from 25s on 2026-07-25. The HMM regime shadow costs ~32s
        # on its FIRST call of a cycle (two Baum-Welch fits; cached per cycle
        # thereafter, so tickers 2..N are ~free). At 25s the whole block timed
        # out and failed open, silently dropping GARCH, HRP and the sizing
        # bracket as well — a degrade that logs an EMPTY exception message and
        # is otherwise invisible. Budget the first-call cost explicitly.
        quant_math = await asyncio.wait_for(
            asyncio.to_thread(build_quant_math_block, ticker, bot_id, cycle_id),
            timeout=60,
        )
        if quant_math:
            desk.cycle_metadata["quant_math_context"] = quant_math
            logger.info("[V3] %s: precomputed quant math injected (%d chars)",
                        ticker, len(quant_math))
    except asyncio.TimeoutError:
        # asyncio.TimeoutError stringifies to "" — the original handler logged
        # "failed (non-fatal): " with nothing after it, which is how a
        # cycle-wide loss of GARCH/HRP/sizing context went unnoticed. Name it.
        logger.warning(
            "[V3] %s: quant math precompute TIMED OUT after 60s — GARCH, HRP and "
            "the sizing bracket are all MISSING from this desk", ticker,
        )
    except Exception as e:
        logger.warning("[V3] %s: quant math precompute failed (non-fatal): %s (%s)",
                       ticker, e, type(e).__name__)

    # Verified technical baseline (2026-07-24 audit): the quant desk was
    # reporting RSI/ATR/SMA values that matched nothing on the desk in 56% of
    # reports. The authoritative numbers live in the `technicals` table — put
    # them in front of the agent instead of hoping it calls a tool (it makes
    # none in 84% of runs).
    try:
        from app.quant.technical_baseline import build_technical_baseline_block
        tech_block = await asyncio.wait_for(
            asyncio.to_thread(build_technical_baseline_block, ticker),
            timeout=15,
        )
        if tech_block:
            desk.cycle_metadata["technical_baseline_context"] = tech_block
            logger.info("[V3] %s: verified technical baseline injected (%d chars)",
                        ticker, len(tech_block))
        else:
            # Only reachable if the block builder itself returns "" — it no
            # longer does for a missing ticker (it emits an explicit NONE ON
            # FILE section). Logged at WARNING because a silent empty baseline
            # is exactly how ASIC reached the board with no data and no
            # complaint anywhere in the logs.
            logger.warning(
                "[V3] %s: technical baseline came back EMPTY — the desk has no "
                "verified indicator anchor for this ticker", ticker,
            )
    except Exception as e:
        logger.warning("[V3] %s: technical baseline failed (non-fatal): %s", ticker, e)

    # Precomputed valuation math (2026-07-27). Same shape and same reasoning as
    # the technical baseline above, one layer down: before this the pipeline had
    # NO valuation math at all — no DCF, no computed EV/EBITDA — so an agent
    # asked whether a name was overvalued had nothing to anchor on but the
    # price, which is how the 171/305 invented-RSI failure started.
    #
    # 15s, not 60s: this is five PK-prefixed index reads with no model fit in
    # it. The 60s above exists for the HMM's Baum-Welch pass and would only
    # delay the desk here.
    try:
        from app.quant.valuation_block import build_valuation_block
        val_block = await asyncio.wait_for(
            asyncio.to_thread(build_valuation_block, ticker),
            timeout=15,
        )
        if val_block:
            desk.cycle_metadata["valuation_context"] = val_block
            logger.info("[V3] %s: precomputed valuation math injected (%d chars)",
                        ticker, len(val_block))
        else:
            # Unreachable unless the builder regresses — it emits an explicit
            # NONE ON FILE section for a ticker with no fundamentals row rather
            # than "". Logged loudly for the ASIC reason: a silent empty block
            # is indistinguishable from a healthy one downstream.
            logger.warning(
                "[V3] %s: valuation block came back EMPTY — the desk has no "
                "verified multiple or implied growth rate for this ticker", ticker,
            )
    except asyncio.TimeoutError:
        logger.warning(
            "[V3] %s: valuation math precompute TIMED OUT after 15s — enterprise "
            "value, multiples and the reverse DCF are all MISSING from this desk",
            ticker,
        )
    except Exception as e:
        logger.warning("[V3] %s: valuation math precompute failed (non-fatal): "
                       "%s (%s)", ticker, e, type(e).__name__)

    # Precomputed fundamental snapshot (2026-07-28). The third of the same
    # family. The fidelity audit found the fundamental analyst emitted no
    # numeric fields at all across 163 artifacts, so nothing reconciled it and
    # the ratios in its prose went unchecked — 4 of 7 stated P/Es were wrong.
    # It also gives the deciding desks fundamentals as NUMBERS: they previously
    # arrived as prose while technicals arrived as reconciled figures, which is
    # why the synthesizer's overrides leaned on oscillators (stochastic
    # +27.1pp) and away from fundamentals (eps -21.2pp).
    #
    # 10s: a single indexed row read, no model fit and no filing scan.
    try:
        from app.quant.fundamental_block import build_fundamental_block
        fund_block = await asyncio.wait_for(
            asyncio.to_thread(build_fundamental_block, ticker),
            timeout=10,
        )
        if fund_block:
            desk.cycle_metadata["fundamental_context"] = fund_block
            logger.info("[V3] %s: precomputed fundamental snapshot injected "
                        "(%d chars)", ticker, len(fund_block))
        else:
            logger.warning(
                "[V3] %s: fundamental block came back EMPTY — the desk has no "
                "verified ratios for this ticker", ticker,
            )
    except asyncio.TimeoutError:
        logger.warning(
            "[V3] %s: fundamental snapshot precompute TIMED OUT after 10s — "
            "margins, returns, leverage and growth are MISSING from this desk",
            ticker,
        )
    except Exception as e:
        logger.warning("[V3] %s: fundamental snapshot precompute failed "
                       "(non-fatal): %s (%s)", ticker, e, type(e).__name__)

    # Recorded third-party opinion cards (2026-07-27). Unlike every other
    # block built here this one returns "" when there is no coverage, and that
    # is correct: a ticker nobody happened to discuss is not a gap in evidence,
    # and announcing the absence on every desk would teach the agent to read
    # one commentator's silence as information.
    try:
        from app.v3.opinion_block import build_opinion_block
        opinion_block = await asyncio.wait_for(
            asyncio.to_thread(build_opinion_block, ticker),
            timeout=10,
        )
        if opinion_block:
            desk.cycle_metadata["opinion_context"] = opinion_block
            logger.info("[V3] %s: recorded opinion cards injected (%d chars)",
                        ticker, len(opinion_block))
    except Exception as e:
        logger.warning("[V3] %s: opinion block failed (non-fatal): %s", ticker, e)

    # Staleness SHADOW (2026-07-27). Deliberately observational, not a gate.
    # After the collection + refresh-coverage fixes the active watchlist
    # measures 0 tickers blocked by every candidate staleness rule on every
    # simulated session (scripts/simulate_freshness_thresholds.py), so a hard
    # gate here would be dead weight that can only fire on a regression — and
    # a threshold guessed today, against one clean week, is exactly the
    # unfalsifiable edit this investigation kept running into. Record what a
    # gate WOULD have blocked; promote it only when the distribution earns it.
    try:
        from app.quant.technical_baseline import compute_technical_baseline

        _b = await asyncio.wait_for(
            asyncio.to_thread(compute_technical_baseline, ticker), timeout=10,
        )
        _trd = (_b or {}).get("age_trading_days")
        if isinstance(_trd, int) and _trd > 3:
            from app.v3.telemetry import record_guardrail_firing

            record_guardrail_firing(
                "SHADOW_STALE_PRICE_DATA",
                ticker=ticker,
                cycle_id=cycle_id or "",
                detail={
                    "age_trading_days": _trd,
                    "as_of": str((_b or {}).get("as_of")),
                    "shadow": True,
                    "would_block": True,
                },
            )
            logger.info(
                "[V3] %s: SHADOW stale-data flag — baseline is %d trading day(s) "
                "old (observational, trade NOT blocked)", ticker, _trd,
            )
    except Exception as e:  # noqa: BLE001 — a shadow must never affect a cycle
        logger.debug("[V3] %s: staleness shadow skipped: %s", ticker, e)

    # Alternative data (2026-07-23 collector wave): insider cluster buys +
    # social chatter, precomputed for the research analysts — same rationale
    # as the quant block, the data must be ON the desk, not behind a tool.
    try:
        from app.v3.alt_data_block import build_alt_data_block
        alt_block = await asyncio.wait_for(
            asyncio.to_thread(build_alt_data_block, ticker),
            timeout=10,
        )
        if alt_block:
            desk.cycle_metadata["alt_data_context"] = alt_block
            logger.info("[V3] %s: alt-data block injected (%d chars)", ticker, len(alt_block))
    except Exception as e:
        logger.warning("[V3] %s: alt-data precompute failed (non-fatal): %s", ticker, e)

    # Book-level brief (2026-07-23): every decision was single-ticker — no
    # agent saw net exposure, concentration, sector tilt, or the candidate's
    # correlation to held positions. Injected for quant + board.
    try:
        from app.v3.book_brief import build_book_brief
        book_brief = await asyncio.wait_for(
            asyncio.to_thread(build_book_brief, ticker, bot_id),
            timeout=20,
        )
        if book_brief:
            desk.cycle_metadata["book_brief_context"] = book_brief
            logger.info("[V3] %s: book brief injected (%d chars)", ticker, len(book_brief))
    except Exception as e:
        logger.warning("[V3] %s: book brief failed (non-fatal): %s", ticker, e)

    # Autoresearch directives — global ones plus any targeting this ticker.
    # The param existed since V3 launch but was never consumed; directives
    # were write-only (janitor-deleted). Non-fatal, capped to stay small.
    if active_directives:
        try:
            relevant = [
                d for d in active_directives
                if not d.get("target_ticker")
                or (d.get("target_ticker") or "").upper() == ticker.upper()
            ][:6]
            if relevant:
                lines = [
                    f"- [{d.get('severity', 'info').upper()}] "
                    f"({d.get('directive_type', 'note')}) {d.get('directive_text', '')}"
                    for d in relevant
                ]
                desk.cycle_metadata["directives_context"] = "\n".join(lines)[:1500]
                logger.info("[V3] %s: injected %d autoresearch directives",
                            ticker, len(relevant))
        except Exception as dir_err:
            logger.debug("[V3] %s: directive injection failed (non-fatal): %s",
                         ticker, dir_err)

    # Retrieve past cycle memory for this ticker (non-fatal)
    try:
        from app.services.memory.retriever import MemoryRetriever
        retrieval_results = MemoryRetriever.retrieve(ticker=ticker)
        brief_text = ""
        if retrieval_results:
            memory_brief = MemoryRetriever.build_memory_brief(retrieval_results)
            brief_text = memory_brief.get("brief_text", "")

        # Working-memory (reminders/facts/patterns) + hybrid semantic recall.
        # These were previously injected only via the dead RLM prompt path and
        # never reached live agents. Char-capped inside the builders.
        addenda = ""
        try:
            from app.services.retrieval_context import build_memory_addenda
            addenda = build_memory_addenda(ticker)
        except Exception as addenda_err:
            logger.debug("[V3] %s: memory addenda failed (non-fatal): %s",
                         ticker, addenda_err)

        combined = "\n\n".join(b for b in (brief_text, addenda) if b)
        if combined:
            desk.cycle_metadata["memory_context"] = combined
            logger.info(
                "[V3] %s: Injected memory context (%d canonical entries, %d chars total)",
                ticker, len(retrieval_results or []), len(combined),
            )
    except Exception as e:
        logger.warning("[V3] %s: Memory retrieval failed (non-fatal): %s", ticker, e)

    # Retrieve the previous cycle's SharedDesk ("Manila Envelope")
    # NOTE: Load ONCE and reuse for both envelope injection and triage gate
    previous_desk = None
    try:
        from app.v3.desk_persistence import load_latest_desk_for_ticker
        previous_desk = load_latest_desk_for_ticker(ticker)
        if previous_desk:
            # Compact structured brief (~400 chars), not the full 8K narrative —
            # continuity needs the decision + headline findings only (plan 4.4).
            prev_context = previous_desk.get_handoff_brief()
            if prev_context and prev_context != "No artifacts on desk yet.":
                desk.cycle_metadata["previous_desk_context"] = prev_context
                
                # Calculate days old for logging
                from app.utils.tz import ensure_aware
                days_old = -1
                dt = ensure_aware(previous_desk.created_at)
                if dt is not None:
                    days_old = (datetime.now(timezone.utc) - dt).days
                
                logger.info(
                    "[V3] %s: Injected previous SharedDesk context from %d days ago (%d chars)",
                    ticker, days_old, len(prev_context)
                )
    except Exception as e:
        logger.warning("[V3] %s: Failed to load previous SharedDesk (non-fatal): %s", ticker, e)

    emit(
        "analyzing", f"v3_ctx_{ticker}",
        f"📋 {ticker}: SharedDesk created, cycle metadata & data report injected",
        status="ok",
    )

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 0: Triage Gate
    # ═══════════════════════════════════════════════════════════════════
    from app.config import settings
    triage_tier = "v3_full"
    if settings.TRIAGE_ENABLED:
        try:
            from app.db.connection import get_db
            with get_db() as db:
                news_count = db.execute(
                    "SELECT COUNT(*) FROM news_articles WHERE ticker = %s AND published_at >= NOW() - INTERVAL '24 hours'",
                    [ticker]
                ).fetchone()[0]
        except Exception as e:
            logger.warning("[V3] %s: Triage news_count query failed (defaulting to 0): %s", ticker, e)
            news_count = 0

        hours_old = 9999
        if desk.cycle_metadata.get("previous_desk_context") and previous_desk:
            try:
                from app.utils.tz import ensure_aware
                dt = ensure_aware(previous_desk.created_at)
                if dt is not None:
                    hours_old = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            except Exception as e:
                logger.warning("[V3] %s: Triage hours_old calculation failed (defaulting to 9999): %s", ticker, e)

        from app.services.parameter_store import get_param as _get_param

        # A standing cross-agent contradiction on the prior desk (fundamental
        # vs quant/tournament dissent recorded by the contradiction shadow) is
        # exactly the case one cheap delta agent should NOT re-affirm alone —
        # force the full panel so the disagreement gets re-argued.
        prior_contradictions = 0
        try:
            for _t in (getattr(previous_desk, "agent_telemetry", None) or []):
                if isinstance(_t, dict) and _t.get("contradiction_count"):
                    prior_contradictions = int(_t.get("contradiction_count") or 0)
        except Exception:
            prior_contradictions = 0

        if hours_old >= _get_param("TRIAGE_DEEP_HOURS") or news_count >= _get_param("TRIAGE_DEEP_NEWS_VOLUME"):
            triage_tier = "v3_deep"
        elif prior_contradictions > 0 and hours_old > _get_param("TRIAGE_GLANCE_HOURS") / 8:
            triage_tier = "v3_deep"
            logger.info(
                "[V3] %s: Triage escalated to deep — prior desk carried %d unresolved "
                "cross-agent contradiction(s)", ticker, prior_contradictions,
            )
        elif hours_old <= _get_param("TRIAGE_GLANCE_HOURS") and news_count == 0:
            # Recently analysed AND nothing new at all → hard skip (cheapest).
            triage_tier = "v3_glance"
        else:
            # Recently-ish analysed with some (sub-deep) news, or a modest-age
            # re-look → the Delta Analyst does ONE cheap pass instead of the full
            # panel, escalating only if it finds a material change. (Previously
            # this band ran the full panel or was glance-skipped even with news.)
            triage_tier = "v3_delta"

        emit("analyzing", f"v3_triage_{ticker}", f"🚦 {ticker}: Triage Gate evaluated → {triage_tier} (News: {news_count}, Age: {int(hours_old)}h)", status="ok")
        
        if triage_tier == "v3_glance":
            logger.info("[V3] %s: Skipped by Triage Gate (GLANCE tier)", ticker)
            desk.append_artifact("final_decision", {
                "action": "HOLD",
                "confidence": 0,
                "reasoning": f"Skipped by Triage Gate (Age: {int(hours_old)}h, News: {news_count}). No new catalysts.",
                "persona_used": "Triage Gate",
                # 2026-07-25: this HOLD is a pure age/news-count heuristic that
                # runs BEFORE any agent. Unstamped, it inherited board_reasoned
                # and the scorecard counted it as a real board opinion — a HOLD
                # nobody reasoned about, scored as evidence about the board.
                "decision_provenance": DecisionProvenance.TRIAGE_SKIP.value,
            })
            # NOTE: Do NOT advance phase here. The only valid transitions from
            # INIT are RESEARCH_DONE and ABORTED. A glance-skipped ticker never
            # ran research/debate/decision so advancing to PM_DONE is invalid.
            # The desk stays at INIT which is correct for a skipped ticker.
            save_desk(desk)
            elapsed_s = time.monotonic() - t_pipeline
            result = _build_v1_compatible_result(desk, elapsed_s=elapsed_s)
            result["triage_tier"] = triage_tier
            result["escalated"] = False
            return result

        # ── Delta tier: ONE cheap agent re-looks the prior thesis vs what
        # changed, and escalates to the full panel only if the change is
        # material. This is the energy saver for re-looks / Watch Desk wakes.
        if triage_tier == "v3_delta":
            from app.v3.agents import delta_analyst
            emit(
                "analyzing", f"v3_delta_start_{ticker}",
                f"⚡ {ticker}: Delta re-look — one agent checks the prior thesis vs "
                f"what changed (skips the full panel unless material)",
                status="ok",
            )
            try:
                delta_outcome = await _run_agent_with_circuit_breaker(
                    desk, delta_analyst, "delta_analyst", breaker, cycle_id, bot_id, emit,
                )
            except Exception as de:
                logger.warning("[V3] %s: Delta agent errored (%s) — escalating to full panel", ticker, de)
                delta_outcome = None

            delta = desk.delta_report or {}
            verdict = str(delta.get("verdict") or "").upper()
            # Conservative: escalate on ESCALATE, an explicit escalate flag, an empty
            # / failed delta, or any non-success outcome. Never rubber-stamp.
            escalate = (
                not delta
                or bool(delta.get("escalate"))
                or verdict == "ESCALATE"
                or delta_outcome != PhaseOutcome.SUCCESS
            )

            if not escalate:
                d_action = str(delta.get("action") or "HOLD").upper()
                d_conf = int(delta.get("confidence") or 0)
                _delta_decision = {
                    "summary": delta.get("summary", f"Delta re-look: {verdict or 'REAFFIRM'}"),
                    "action": d_action,
                    "confidence": d_conf,
                    "reasoning": delta.get("reasoning", "Prior thesis reaffirmed by the delta re-look."),
                    "persona_used": "Delta Analyst",
                    "regime": (desk.regime_classification or {}).get("regime", "delta_relook"),
                    "stop_loss": delta.get("stop_loss"),
                    "take_profit": delta.get("take_profit"),
                    "exit_style": delta.get("exit_style"),
                    "dynamic_trigger": delta.get("dynamic_trigger"),
                    "position_size_pct": delta.get("position_size_pct"),
                    # An agent DID decide this one. Stamped explicitly because
                    # this path bypasses agent_runner (where the board's own
                    # stamp is applied), and an unstamped decision is treated
                    # as unattributed — which would silently drop the
                    # pipeline's highest-volume route out of every accuracy
                    # measurement.
                    "decision_provenance": DecisionProvenance.BOARD_REASONED.value,
                }
                # The delta tier used to write this artifact and return before
                # Layer 6, so the no-shorting guard and EVERY policy gate were
                # skipped on the pipeline's highest-volume route. Apply the same
                # guard the full panel applies (2026-07-25 audit).
                from app.v3.agent_runner import guard_unshortable_sell
                _delta_decision = guard_unshortable_sell(
                    _delta_decision, desk=desk, bot_id=bot_id,
                )
                desk.append_artifact("final_decision", _delta_decision)
                emit(
                    "analyzing", f"v3_delta_done_{ticker}",
                    f"⚡ {ticker}: Delta {verdict or 'REAFFIRM'} → {d_action}@{d_conf}% "
                    f"(full panel skipped — energy saved)",
                    status="ok",
                )
                logger.info(
                    "[V3] %s: Delta re-look %s → %s@%d%% (full panel skipped)",
                    ticker, verdict or "REAFFIRM", d_action, d_conf,
                )
                # Delta cycles used to leave NO memory trace — a re-affirmed
                # thesis never became an episodic observation, so the memory
                # system was blind to every energy-saved cycle.
                try:
                    from app.services.memory.store import MemoryStore
                    MemoryStore().add_episodic_observation({
                        "cycle_id": cycle_id,
                        "ticker": ticker,
                        "source_type": "v3_delta",
                        "observation_text": (
                            f"Delta re-look for {ticker}: {verdict or 'REAFFIRM'} → "
                            f"{d_action} @ {d_conf}% confidence. "
                            f"{str(delta.get('reasoning') or '')[:400]}"
                        ),
                        "confidence_at_creation": d_conf / 100.0 if d_conf else 0.0,
                        "outcome_label": d_action,
                    })
                except Exception as mem_err:
                    logger.warning("[V3] %s: Delta memory write failed (non-fatal): %s", ticker, mem_err)
                save_desk(desk)
                elapsed_s = time.monotonic() - t_pipeline
                result = _build_v1_compatible_result(desk, elapsed_s=elapsed_s)
                result["triage_tier"] = "v3_delta"
                result["escalated"] = False
                # Layer 6 lives at the end of the full-panel flow, which this
                # early return skips. Without this, `policy_action` is unset and
                # pipeline_service's enforcement branches (which key off it)
                # cannot match — so a delta BUY skipped the low-confidence,
                # missing-regime and strategy-health CUT gates entirely, and was
                # sized without the consensus/data-quality haircuts.
                policy_action = _apply_policy_gates(desk)
                result["policy_action"] = policy_action
                try:
                    _persist_policy_action(cycle_id, ticker, policy_action)
                except Exception as pe:
                    logger.warning("[V3] %s: delta policy_action persist failed (non-fatal): %s", ticker, pe)
                if policy_action.startswith("HOLD_") and d_action in ("BUY", "SELL"):
                    logger.warning(
                        "[V3] %s: delta %s @ %d%% BLOCKED by policy gate → %s",
                        ticker, d_action, d_conf, policy_action,
                    )
                    emit(
                        "analyzing", f"v3_policy_blocked_{ticker}",
                        f"🚫 {ticker}: {d_action} @ {d_conf}% BLOCKED by policy gate "
                        f"→ {policy_action}",
                        status="warning",
                    )
                return result

            # Material change (or no usable delta) → fall through to the full panel.
            emit(
                "analyzing", f"v3_delta_escalate_{ticker}",
                f"⚡ {ticker}: Delta found a material change → escalating to the full panel",
                status="ok",
            )
            logger.info(
                "[V3] %s: Delta re-look ESCALATED (%s) → running full panel",
                ticker, delta.get("material_change", "material change or no prior thesis"),
            )
            triage_tier = "v3_delta_escalated"
            # continue below to the full blackboard panel

    # ═══════════════════════════════════════════════════════════════════
    # DYNAMIC BLACKBOARD / P2P COORDINATOR
    # ═══════════════════════════════════════════════════════════════════
    from app.v3.agents import regime_engine
    from app.v3.agents import junior_analyst, fundamental_analyst, quant_analyst
    from app.v3.agents import valuation_analyst
    from app.v3.agents import bull_agent, bear_agent, debate_judge
    from app.v3.agents import decision_agent
    from app.config.config_cognition import cognition_settings as _cog_settings
    from app.config import settings as _settings

    tasks_to_run = []
    
    # Track execution counts to prevent infinite cascades / loops
    MAX_RUNS_PER_AGENT = 3
    run_counts = {
        "regime_engine": 0,
        "junior_analyst": 0,
        "fundamental_analyst": 0,
        "quant_analyst": 0,
        "valuation_analyst": 0,
        "bull_argument": 0,
        "bear_rebuttal": 0,
        "debate_judge": 0,
        "board_of_directors": 0,
        "decision_synthesizer": 0,
        "tournament_debate": 0,
    }

    regime = "CONTRADICTORY"
    fa_skipped = False  # set when the Regime Engine recommends skipping FA
    # Dispatch-once latches for the decision layer. Peer-requested analyst
    # re-runs (request_peer_analysis) re-write the research sections, which
    # would otherwise re-fire the whole debate→board→synth chain every time
    # (observed live: 1 ticker → tournament×2, board×2, synth×2, ~2x compute).
    # The debate consumes a SNAPSHOT of research; re-running analysts after it
    # has started cannot change a verdict already rendered, so we latch each
    # decision-layer stage to a single dispatch.
    debate_dispatched = False
    board_dispatched = False
    synth_dispatched = False
    peer_drop_logged = False

    def _queue_agent(name: str, module: Any, query: str = "", parent: str = ""):
        if run_counts.get(name, 0) >= MAX_RUNS_PER_AGENT:
            logger.warning("[V3] Max runs reached for %s. Skipping trigger to prevent loops.", name)
            return
        
        # Check if already pending to prevent duplicate queue entries
        if any(t["name"] == name and t["query"] == query for t in tasks_to_run):
            return
            
        tasks_to_run.append({
            "name": name,
            "module": module,
            "query": query,
            "parent": parent
        })
        logger.info("[V3] Queued dynamic task: %s (query='%s', parent='%s')", name, query, parent)

    async def whiteboard_subscriber(event):
        nonlocal regime, fa_skipped, debate_dispatched, board_dispatched, synth_dispatched
        # The bus delivers only this ticker's events (subscription is
        # ticker-scoped), but keep the filter as defense in depth against
        # unscoped publishers — a cross-ticker event here would cross-trigger
        # duplicate queued tasks and re-runs of completed agents.
        event_ticker = (event.get("ticker") or "").upper()
        if event_ticker and event_ticker != ticker.upper():
            return
        # Same ticker from another cycle (or the legacy default_cycle board)
        # must not trigger this cycle's agent chain. Strict: an event with NO
        # cycle_id is rejected too — every real publisher stamps one.
        if (event.get("cycle_id") or "") != cycle_id:
            return
        # Only section WRITES drive the agent chain. Annotations
        # ("whiteboard_annotation") carry the annotated entry's section but no
        # content — letting one fall through would reset regime to
        # CONTRADICTORY, re-queue FA/QA, or re-trigger the debate chain.
        if event.get("type") != "whiteboard_update":
            return
        sec = event.get("section")
        auth = event.get("author")
        logger.info("[V3] Whiteboard event trigger: section '%s' updated by '%s'", sec, auth)
        
        if sec == "regime_classification":
            content = event.get("content") or {}
            regime = content.get("regime", "CONTRADICTORY")

            # The Regime Engine owns the skip decision (plan 1.3): honor its
            # suggested_pipeline_modifications instead of hardcoding on the
            # regime label. An artifact WITHOUT the field (older prompt or
            # partial output) keeps the legacy HIGH_VOLATILITY heuristic.
            mods = content.get("suggested_pipeline_modifications")
            skip_fa = _regime_recommends_skip_fa(content)

            if skip_fa:
                fa_skipped = True
                logger.info(
                    "[V3] Regime Engine recommends skipping Fundamental Analyst "
                    "(regime=%s, mods=%s). Running JA & QA only.", regime, mods,
                )
                desk.append_artifact("fundamental_report", {
                    "summary": (
                        "Fundamental analysis skipped on the Regime Engine's "
                        f"recommendation (regime: {regime}). Quantitative metrics prioritized."
                    ),
                    "pillars": {
                        "revenue_growth": "Not analyzed", "profitability": "Not analyzed",
                        "moat": "Not analyzed", "management": "Not analyzed", "valuation": "Not analyzed"
                    },
                    "thesis_direction": "NEUTRAL",
                    "confidence": 50,
                    "data_gaps": ["DataGap: Fundamental analysis bypassed"],
                    "catalysts": [],
                    "risks": []
                })
                breaker.record_outcome("fundamental_analyst", PhaseOutcome.SUCCESS)

                _queue_agent("junior_analyst", junior_analyst, parent="regime_engine")
                _queue_agent("quant_analyst", quant_analyst, parent="regime_engine")
            else:
                _queue_agent("junior_analyst", junior_analyst, parent="regime_engine")

        elif sec == "desk_note":  # junior_analyst completed
            # JA is the first real intelligence gate (plan 2.2): honor its
            # triage_recommendation. Anything unrecognized behaves as FULL.
            triage = str((event.get("content") or {}).get("triage_recommendation") or "FULL").upper()

            # A triage that shortens the pipeline is only valid if the analyst
            # could actually see. When its tools failed, "no catalysts found"
            # is a statement about our plumbing, not about the company — so
            # downgrade to FULL rather than bank the saving. FULL is unaffected;
            # this can only ever ADD work.
            if triage in ("SKIP", "QUANT_ONLY"):
                _why = research_degraded(cycle_id, ticker, event.get("content") or {})
                if _why:
                    logger.warning(
                        "[V3] %s: JA triage %s OVERRIDDEN to FULL — research was "
                        "degraded (%s).", ticker, triage, _why,
                    )
                    emit("analyzing", f"v3_triage_degraded_{ticker}",
                         f"⚠️ {ticker}: triage {triage} overridden → FULL "
                         f"(degraded research: {_why})", status="warning")
                    desk.append_artifact("degradation_note", {
                        "stage": "junior_analyst_triage",
                        "requested_triage": triage,
                        "applied_triage": "FULL",
                        "reason": _why,
                    })
                    triage = "FULL"

            if triage == "SKIP":
                logger.info("[V3] %s: JA triage says SKIP — ending pipeline (no catalysts).", ticker)
                # Drop anything already queued (e.g. QA pre-queued by a
                # regime-engine skip_fa path) — SKIP ends the pipeline.
                tasks_to_run.clear()
                # Local append only (no whiteboard write) so the synthesizer
                # is NOT chained — mirrors the Triage Gate's early HOLD.
                desk.append_artifact("final_decision", {
                    "action": "HOLD",
                    "confidence": 0,
                    "reasoning": (
                        "Junior Analyst triage: no new catalysts since the previous "
                        f"cycle. JA summary: {(event.get('content') or {}).get('summary', '')[:300]}"
                    ),
                    "persona_used": "junior_analyst_triage",
                    # 2026-07-25: the JA recommended a SKIP; no board reasoned
                    # about an action. Unstamped, this HOLD@0 was credited to
                    # the board and scored as a genuine decision.
                    "decision_provenance": DecisionProvenance.TRIAGE_SKIP.value,
                })
                emit("analyzing", f"v3_ja_triage_{ticker}",
                     f"🚦 {ticker}: JA triage → SKIP (no new catalysts)", status="ok")
            elif triage == "QUANT_ONLY" and not fa_skipped:
                fa_skipped = True
                logger.info("[V3] %s: JA triage says QUANT_ONLY — skipping Fundamental Analyst.", ticker)
                desk.append_artifact("fundamental_report", {
                    "summary": (
                        "Fundamental analysis skipped on the Junior Analyst's triage "
                        "recommendation (QUANT_ONLY): no qualitative catalysts found."
                    ),
                    "pillars": {
                        "revenue_growth": "Not analyzed", "profitability": "Not analyzed",
                        "moat": "Not analyzed", "management": "Not analyzed", "valuation": "Not analyzed"
                    },
                    "thesis_direction": "NEUTRAL",
                    "confidence": 50,
                    "data_gaps": ["DataGap: Fundamental analysis bypassed"],
                    "catalysts": [],
                    "risks": []
                })
                breaker.record_outcome("fundamental_analyst", PhaseOutcome.SUCCESS)
                _queue_agent("quant_analyst", quant_analyst, parent="junior_analyst")
            elif not fa_skipped:
                _queue_agent("fundamental_analyst", fundamental_analyst, parent="junior_analyst")
                _queue_agent("quant_analyst", quant_analyst, parent="junior_analyst")
                # Queued HERE, with FA and QA, and deliberately NOT added to the
                # _queue_debate_phase() gate below. The scheduler is FIFO
                # (_queue_agent appends, the loop pops index 0), so a task queued
                # now necessarily runs before bull/bear, which can only be
                # appended later by _queue_debate_phase. That buys the ordering
                # without buying the deadlock: a third term in an AND-gate would
                # need a matching synthetic artifact in every skip path, and the
                # two that already exist for fa_skipped are the evidence of how
                # easily one gets missed.
                #
                # Not queued on the QUANT_ONLY / regime-skip-FA paths: valuation
                # IS fundamental analysis, so it follows FA's fate rather than
                # outliving it.
                _queue_agent("valuation_analyst", valuation_analyst, parent="junior_analyst")

        elif sec in ("fundamental_report", "quant_report"):
            # Check if research tier is fully complete
            if fa_skipped:
                if desk.has_artifact("desk_note") and desk.has_artifact("quant_report"):
                    _queue_debate_phase()
            else:
                if desk.has_artifact("desk_note") and desk.has_artifact("fundamental_report") and desk.has_artifact("quant_report"):
                    _queue_debate_phase()
                    
        elif sec in ("bull_argument", "bear_rebuttal"):
            if desk.has_artifact("bull_argument") and desk.has_artifact("bear_rebuttal"):
                _queue_agent("debate_judge", debate_judge, parent="bull_argument")
                
        elif sec in ("debate_judge", "tournament_result"):
            if not board_dispatched:
                board_dispatched = True
                _queue_agent("board_of_directors", None, parent="debate_judge")

        elif sec == "final_decision":
            if _settings.DECISION_AGENT_ENABLED and not synth_dispatched:
                synth_dispatched = True
                # Deep decomposed recall for the synthesizer, only when the
                # debate verdict is low-confidence/conflicted — one extra
                # small LLM call + a few retrievals, justified exactly where
                # signals disagree. Non-fatal; synthesizer runs without it.
                try:
                    verdict = desk.debate_judge or {}
                    v_conf = int(verdict.get("confidence") or 0)
                    if v_conf < 60:
                        from app.services.retrieval_decomposed import build_decomposed_block
                        deep_block = await build_decomposed_block(
                            ticker,
                            f"What are the key risks, catalysts, and conflicting "
                            f"signals for {ticker}?",
                        )
                        if deep_block:
                            desk.cycle_metadata["deep_retrieval_context"] = deep_block
                            logger.info(
                                "[V3] %s: deep retrieval injected for synthesizer "
                                "(verdict confidence %d)", ticker, v_conf,
                            )
                except Exception as deep_err:
                    logger.debug("[V3] %s: deep retrieval failed (non-fatal): %s",
                                 ticker, deep_err)
                _queue_agent("decision_synthesizer", decision_agent, parent="board_of_directors")

    def _queue_debate_phase():
        nonlocal debate_dispatched, board_dispatched
        # Latch: the debate runs once on a research snapshot. A peer-requested
        # analyst re-run that re-writes fundamental_report/quant_report must
        # NOT re-queue the (expensive, ~8min) tournament.
        if debate_dispatched:
            return
        debate_dispatched = True

        if desk.phase == DeskPhase.INIT:
            desk.advance_phase(DeskPhase.RESEARCH_DONE)
            save_desk(desk)
            emit("analyzing", f"v3_research_done_{ticker}", f"📊 {ticker}: Research layer complete", status="ok")

        # skip_debate (2026-07-21, audit item 8): honored ONLY in true panic —
        # the regime engine suggested it AND its own volatility score is ≥0.9.
        # Skipping removes the jury veto, so the skip is recorded as a standing
        # risk flag: _apply_policy_gates then demands full mitigation
        # (stop_loss + dynamic_trigger + position_size_pct) for any trade,
        # exactly like a solo-juror veto. The ~9min tournament is the cycle's
        # largest time cost; in a dislocating tape speed can beat deliberation.
        regime_art = desk.regime_classification or {}
        mods = regime_art.get("suggested_pipeline_modifications") or []
        vol_factor = (regime_art.get("factors") or {}).get("volatility")
        if (
            "skip_debate" in mods
            and isinstance(vol_factor, (int, float))
            and vol_factor >= 0.9
        ):
            logger.warning(
                "[V3] %s: DEBATE SKIPPED by Regime Engine (volatility %.2f) — "
                "risk flag recorded, board must fully mitigate any trade",
                ticker, vol_factor,
            )
            skip_summary = (
                f"Debate SKIPPED by the Regime Engine (volatility {vol_factor:.2f} "
                "≥ 0.90 panic threshold — speed over deliberation). There is NO "
                "jury protection this cycle: any trade must carry full mitigation "
                "(stop_loss, dynamic_trigger, position_size_pct) or the policy "
                "gate holds it."
            )
            desk.append_artifact("tournament_result", {
                "summary": skip_summary,
                "action": "HOLD",
                "confidence": 0,
                "winning_side": "skipped",
                "pitches": [], "survivors": [], "h2h": {}, "jury_verdict": {},
                "vetoed": False,
                "skipped": True,
                "risk_flags": ["debate_skipped_by_regime"],
                "total_tokens": 0,
                # No debate ran, so there is no verdict to shadow — but the
                # field must exist on every artifact or the P&L split silently
                # drops the skip paths instead of excluding them by `skipped`.
                "shadow_mode": False,
            })
            desk.append_artifact("debate_judge", {
                "summary": skip_summary,
                "action": "HOLD",
                "confidence": 0,
                "winning_side": "skipped",
                "source": "tournament_debate",
            })
            emit(
                "analyzing", f"v3_debate_skipped_{ticker}",
                f"⚡ {ticker}: Debate skipped by Regime Engine (volatility {vol_factor:.2f}) — unmitigated-risk gate armed",
                status="ok",
            )
            if not board_dispatched:
                board_dispatched = True
                _queue_agent("board_of_directors", None, parent="regime_engine")
            return

        # ── No-trade-available gate (2026-07-24 audit) ──
        # There is no shorting: on a ticker the bot does not hold, the only
        # executable outcome is BUY. When BOTH research desks came back
        # BEARISH, the debate is being asked to find a buy case that its own
        # inputs unanimously reject — and the tournament is the single most
        # expensive stage in the pipeline (~246s).
        #
        # Measured over 5 weeks this fires on 100 desks: 73 unactionable SELLs
        # and 24 no-op HOLDs. It does NOT skip the Board — 3 of those 100 did
        # end in a BUY, and while all 3 underperformed the always-long baseline
        # (+2.55% vs +4.05%), a gate that silently deletes profitable trades on
        # n=3 is not one to install. The board still decides; it just decides
        # without a debate it had no material chance of using.
        if (
            _settings_no_trade_gate_enabled()
            and desk.cycle_metadata.get("held") is False
            and _research_unanimously_bearish(desk)
        ):
            logger.info(
                "[V3] %s: no-trade-available gate — unheld + research unanimously "
                "BEARISH; skipping the tournament, board still decides", ticker,
            )
            emit(
                "analyzing", f"v3_no_trade_gate_{ticker}",
                f"⏭️ {ticker}: unheld and research is unanimously bearish — "
                f"no buy case to debate, skipping to the Board",
                status="ok",
            )
            skip_note = (
                "Debate SKIPPED: this ticker is NOT held and both research "
                "desks returned BEARISH. With no shorting, the only executable "
                "action here is BUY, which the research unanimously rejects. "
                "You may still call BUY if you believe the research is wrong — "
                "but say why explicitly, because no debate stress-tested it."
            )
            desk.append_artifact("tournament_result", {
                "summary": skip_note, "action": "HOLD", "confidence": 0,
                "winning_side": "skipped", "pitches": [], "survivors": [],
                "h2h": {}, "jury_verdict": {}, "vetoed": False, "skipped": True,
                "risk_flags": ["debate_skipped_no_trade_available"],
                "total_tokens": 0,
                "shadow_mode": False,  # no debate ran; see the regime skip above
            })
            desk.append_artifact("debate_judge", {
                "summary": skip_note, "action": "HOLD", "confidence": 0,
                "winning_side": "skipped", "source": "no_trade_available_gate",
            })
            # The board still genuinely decides after this gate, so its
            # provenance stays BOARD_REASONED — but it decided without a
            # debate, and scoring should be able to tell those apart.
            desk.cycle_metadata["debate_skipped_by_gate"] = True
            if not board_dispatched:
                board_dispatched = True
                _queue_agent("board_of_directors", None, parent="quant_analyst")
            return

        if _cog_settings.TOURNAMENT_MODE:
            _queue_agent("tournament_debate", None, parent="quant_analyst")
        else:
            _queue_agent("bull_argument", bull_agent, parent="quant_analyst")
            _queue_agent("bear_rebuttal", bear_agent, parent="quant_analyst")

    async def _has_pending_peer_requests() -> bool:
        # Peer requests are a RESEARCH-phase mechanism: an analyst asking a
        # sibling for a specific data point before the debate. Once the debate
        # has been dispatched, a late request cannot inform the verdict — and
        # honoring it re-runs an analyst whose output nothing downstream reads.
        if debate_dispatched:
            return False
        try:
            task_section = await whiteboard.get_section(ticker=ticker, cycle_id=cycle_id, section="task_queue")
            if task_section and isinstance(task_section.get("content"), dict):
                tasks_list = task_section["content"].get("tasks", [])
                return any(t.get("status") == "pending" for t in tasks_list)
        except Exception as e:
            logger.warning("[V3] Error checking pending peer requests: %s", e)
        return False

    async def _process_peer_requests():
        nonlocal peer_drop_logged
        # Do not spawn analyst re-runs once the debate has moved on (see
        # _has_pending_peer_requests). Pending requests are left as-is —
        # but say so ONCE, or the requesting agent's ask vanishes untraceably.
        # (One-shot: this runs every scheduler iteration after dispatch, and
        # each check is a whiteboard DB read.)
        if debate_dispatched:
            if not peer_drop_logged:
                peer_drop_logged = True
                try:
                    task_section = await whiteboard.get_section(
                        ticker=ticker, cycle_id=cycle_id, section="task_queue"
                    )
                    if task_section and isinstance(task_section.get("content"), dict):
                        dropped = [
                            t for t in task_section["content"].get("tasks", [])
                            if t.get("status") == "pending"
                        ]
                        if dropped:
                            logger.info(
                                "[V3] %s: %d peer request(s) dropped — debate already "
                                "dispatched (targets: %s)",
                                ticker, len(dropped),
                                ", ".join(str(t.get("target_agent")) for t in dropped),
                            )
                except Exception:
                    pass
            return
        try:
            task_section = await whiteboard.get_section(ticker=ticker, cycle_id=cycle_id, section="task_queue")
            if task_section and isinstance(task_section.get("content"), dict):
                tasks_list = task_section["content"].get("tasks", [])
                updated = False
                for t in tasks_list:
                    if t.get("status") == "pending":
                        target = t.get("target_agent")
                        query_text = t.get("query")
                        requester = t.get("requested_by")
                        
                        target_mod = {
                            "junior_analyst": junior_analyst,
                            "fundamental_analyst": fundamental_analyst,
                            "quant_analyst": quant_analyst
                        }.get(target)
                        
                        if target_mod:
                            _queue_agent(target, target_mod, query=query_text, parent=requester)
                            t["status"] = "running"
                            updated = True
                        else:
                            logger.warning("[V3] Peer request target agent '%s' not recognized.", target)
                            t["status"] = "failed"
                            updated = True
                            
                if updated:
                    await whiteboard.write_section(
                        ticker=ticker,
                        cycle_id=cycle_id,
                        section="task_queue",
                        content={"tasks": tasks_list},
                        author_agent="system"
                    )
        except Exception as e:
            logger.warning("[V3] Process peer requests failed: %s", e)

    async def _execute_tournament_debate(parent: str):
        emit(
            "analyzing", f"v3_tournament_{ticker}",
            f"🏆 {ticker}: Tournament Debate starting (4-stage pipeline)",
            status="running",
            data={"parent": parent} if parent else None
        )
        t_tournament = time.monotonic()
        try:
            from app.cognition.debate.tournament import run_tournament_debate
            from app.cognition.contracts.evidence import EvidencePacket
            from app.cognition.contracts.retrieval import StructuredFact

            # fact_type names are chosen to hit PERSONA_EVIDENCE_FILTER keywords
            # ("fundamental"/"technical"/"news"/"macro"). The old names
            # (desk_note/quant_report) matched NO Technical or Macro keyword, so
            # filter_packet_for_persona fell back to the FULL packet for 3 of 4
            # pitch personas — every persona anchored on the same quant thesis
            # and the tournament produced 4 near-identical pitches.
            # fact_type names are chosen to hit exactly ONE PERSONA_EVIDENCE_FILTER
            # category each — that mapping is the partition, and a fact reachable
            # from two categories means two analysts are not independent.
            #
            # Widened 2026-07-29 for the probabilistic panel: valuation_report was
            # never in the packet at all (the debate could not see the valuation
            # desk), and positioning had no fact of its own, so the 4th analyst
            # would have shared the macro slice.
            facts = []
            for artifact_name, fact_type in (
                ("fundamental_report", "fundamental_report"),
                ("valuation_report", "fundamental_valuation_note"),
                ("quant_report", "technical_quant_report"),
                ("desk_note", "positioning_news_desk_note"),
                ("regime_classification", "macro_regime_note"),
            ):
                artifact = getattr(desk, artifact_name, None)
                if artifact and isinstance(artifact, dict):
                    summary = artifact.get("summary") or artifact.get("rationale") or ""
                    if artifact_name == "regime_classification" and artifact.get("regime"):
                        summary = f"Regime: {artifact['regime']}. {summary}"
                    if summary:
                        facts.append(
                            StructuredFact(
                                fact_type=fact_type,
                                value=summary[:2000],
                                timestamp=datetime.now(timezone.utc),
                            )
                        )

            packet = EvidencePacket(
                entity_id=ticker,
                structured_facts=facts,
                claims=[],
            )

            # DEBATE_ENGINE gates the CALL, not the rendering. The older
            # TOURNAMENT_DEBATE_MODE shadow branch was measured to save ZERO
            # tokens — run_tournament_debate is invoked unconditionally there
            # and only the prompt section is filtered — so the experiment cost
            # the same either way. Exactly one engine runs per ticker.
            #
            # Fail-open to the tournament: a parameter miss must land on today's
            # behaviour, never on an engine nobody chose.
            try:
                from app.services.parameter_store import get_param as _get_engine
                _engine = int(_get_engine("DEBATE_ENGINE"))
            except Exception as _e:  # noqa: BLE001
                logger.warning("[V3] DEBATE_ENGINE lookup failed (%s) — tournament", _e)
                _engine = 0

            if _engine in (1, 2):
                from app.cognition.debate.probabilistic_panel import (
                    run_probabilistic_panel,
                )
                tournament_result = await run_probabilistic_panel(
                    ticker=ticker,
                    packet=packet,
                    cycle_id=cycle_id,
                    bot_id=bot_id,
                    shared_evidence=(_engine == 2),
                )
                logger.info(
                    "[V3] %s: panel P=%.2f spread=%.2f partitioned=%s (%d/%d analysts)",
                    ticker, tournament_result.get("probability", 0.5),
                    tournament_result.get("disagreement", 0.0),
                    tournament_result.get("partitioned"),
                    tournament_result.get("analysts_responded", 0),
                    tournament_result.get("analysts_expected", 0),
                )
            else:
                tournament_result = await run_tournament_debate(
                    ticker=ticker,
                    packet=packet,
                    cycle_id=cycle_id,
                    bot_id=bot_id,
                    position_context=None,
                )

            desk.append_artifact("tournament_result", {
                "summary": tournament_result.get("rationale", "Tournament complete"),
                "action": tournament_result.get("action", "HOLD"),
                "confidence": tournament_result.get("confidence", 0),
                "winning_side": tournament_result.get("winning_side", "split"),
                "pitches": tournament_result.get("pitches", []),
                "survivors": tournament_result.get("survivors", []),
                # h2h carries each thesis's attack_points — the debate nuance
                # the board needs for sizing/stop calibration. Without it the
                # board only ever saw the one-line rationale.
                "h2h": tournament_result.get("h2h", {}),
                "jury_verdict": tournament_result.get("jury_verdict", {}),
                # Read the engine's own `vetoed` first. The tournament reports it
                # inside jury_verdict; the panel has no jury and reports it at the
                # top level, and reading only the nested path would silently drop
                # it to False for any engine that does not have a jury.
                "vetoed": bool(
                    tournament_result.get(
                        "vetoed",
                        (tournament_result.get("jury_verdict") or {}).get("vetoed", False),
                    )
                ),
                "risk_flags": tournament_result.get("risk_flags", []),
                "total_tokens": tournament_result.get("total_tokens", 0),
                # ── probabilistic panel fields (absent for the tournament) ──
                # `probability` is the real signal; `confidence` above is derived
                # from it for backward compatibility. `partitioned` records
                # whether the run actually held information asymmetry — a run
                # where the partition collapsed is N agents reading one packet,
                # and scripts/score_panel.py voids it rather than averaging it in.
                **({"engine": tournament_result["engine"]}
                   if tournament_result.get("engine") else {}),
                **({"probability": tournament_result["probability"]}
                   if tournament_result.get("probability") is not None else {}),
                **({"disagreement": tournament_result.get("disagreement", 0.0),
                    "partitioned": tournament_result.get("partitioned"),
                    "partition_fallbacks": tournament_result.get("partition_fallbacks", {}),
                    "shared_evidence_control": tournament_result.get("shared_evidence_control", False),
                    "views": tournament_result.get("views", []),
                    "analysts_responded": tournament_result.get("analysts_responded", 0),
                    "degraded": tournament_result.get("degraded", False)}
                   if tournament_result.get("engine") == "probabilistic_panel" else {}),
                # Stamped so a later analysis can split realized P&L into
                # cycles where the verdict reached the Board and cycles where
                # it did not. Without this field the shadow experiment is
                # unfalsifiable — the artifact looks identical either way.
                "shadow_mode": tournament_debate_mode() == TOURNAMENT_MODE_SHADOW,
            })

            desk.append_artifact("debate_judge", {
                "summary": tournament_result.get("rationale", ""),
                "action": tournament_result.get("action", "HOLD"),
                "confidence": tournament_result.get("confidence", 0),
                "winning_side": tournament_result.get("winning_side", "split"),
                "source": "tournament_debate",
            })

            # Write tournament_result to whiteboard so subscriber chains board_of_directors
            await whiteboard.write_section(
                ticker=ticker, cycle_id=cycle_id,
                section="tournament_result",
                content=desk.tournament_result,
                author_agent="tournament_debate"
            )

            # ── Structured debate events for the 3D office ──
            # The tournament is otherwise a black box (only start/done reach the
            # office). Replay its stages as discrete, `kind`-tagged events so the
            # War Room can animate pitches → head-to-head clash → jury votes →
            # verdict. Purely additive; never allowed to break the cycle.
            try:
                jury = tournament_result.get("jury_verdict", {}) or {}
                for i, pitch in enumerate(tournament_result.get("pitches", []) or []):
                    persona = pitch.get("persona") or f"pitch_{i}"
                    claim = (pitch.get("claim") or "")[:180]
                    emit(
                        "analyzing", f"v3_debate_pitch_{i}_{ticker}",
                        f"💬 {ticker} debate — {persona}: {claim}",
                        status="running",
                        data={"kind": "debate_pitch", "ticker": ticker,
                              "persona": persona, "claim": claim, "index": i},
                    )
                h2h = tournament_result.get("h2h", {}) or {}
                if h2h:
                    ta = h2h.get("thesis_a", {}) or {}
                    tb = h2h.get("thesis_b", {}) or {}
                    emit(
                        "analyzing", f"v3_debate_clash_{ticker}",
                        f"⚔️ {ticker} head-to-head: "
                        f"{ta.get('persona', 'A')} vs {tb.get('persona', 'B')}",
                        status="running",
                        data={"kind": "debate_clash", "ticker": ticker,
                              "bull": ta, "bear": tb},
                    )
                for juror_name, verdict in (jury.get("jury_results") or {}).items():
                    if not isinstance(verdict, dict):
                        continue
                    winner = verdict.get("winner", "?")
                    score = verdict.get("score", 0)
                    veto = bool(verdict.get("veto", False))
                    emit(
                        "analyzing", f"v3_debate_vote_{juror_name}_{ticker}",
                        f"🗳️ {ticker}: {juror_name} → {winner} "
                        f"({score}/10){' VETO' if veto else ''}",
                        status="running",
                        data={"kind": "debate_vote", "ticker": ticker,
                              "juror": juror_name, "winner": winner,
                              "score": score, "veto": veto},
                    )
                emit(
                    "analyzing", f"v3_debate_verdict_{ticker}",
                    f"⚖️ {ticker} verdict: {tournament_result.get('action', 'HOLD')} "
                    f"@ {tournament_result.get('confidence', 0)}% "
                    f"(winner: {tournament_result.get('winning_side', 'split')})",
                    status="ok",
                    data={"kind": "debate_verdict", "ticker": ticker,
                          "action": tournament_result.get("action", "HOLD"),
                          "confidence": tournament_result.get("confidence", 0),
                          "winning_side": tournament_result.get("winning_side", "split"),
                          "vetoed": tournament_result.get("vetoed", False),
                          "votes": jury.get("votes", {})},
                )
            except Exception as dbg_emit_err:
                logger.warning("[V3] %s: debate event emit failed: %s", ticker, dbg_emit_err)

            emit(
                "analyzing", f"v3_tournament_done_{ticker}",
                f"🏆 {ticker}: Tournament complete → {tournament_result.get('action', 'HOLD')} "
                f"@ {tournament_result.get('confidence', 0)}% "
                f"(winner: {tournament_result.get('winning_side', 'split')})",
                status="ok",
            )
            # The tournament bypasses run_v3_agent, so without this it leaves no
            # v3_agent_telemetry row — which drops its node from the replay flow
            # graph and severs the analyst→board edges (the "islands" bug).
            #
            # Bypassing run_v3_agent also meant bypassing score_artifact, so this
            # was hardcoded to -1: the single most expensive stage in the pipeline
            # (~264s/ticker, ~1.2M tokens per 5-ticker cycle, a third of all agent
            # time) was the only one with no quality signal at all. Score it here
            # instead, so "is the debate worth its cost?" is an answerable question.
            try:
                from app.v3.quality_scorer import score_artifact

                tournament_quality = score_artifact(
                    "tournament_debate", tournament_result
                ).get("quality_score", -1)
            except Exception as score_err:  # noqa: BLE001 — never block the cycle
                logger.warning("[V3] %s: tournament scoring failed: %s", ticker, score_err)
                tournament_quality = -1

            # Size the artifact like every other agent does. Left unset it
            # defaulted to 0, so the single most expensive stage in the cycle
            # (245-305s per ticker in cycle-v3-1785137616, ~30% of per-ticker
            # wall clock) was the one row that could not be checked for
            # "expensive AND empty" — the exact question its own cost invites.
            try:
                _t_bytes = len(json.dumps(tournament_result, default=str))
            except Exception:
                _t_bytes = 0

            desk.record_agent_telemetry({
                "agent_name": "v3_tournament_debate",
                "ticker": ticker,
                "elapsed_ms": int((time.monotonic() - t_tournament) * 1000),
                "loops_used": 1,
                "token_usage": int(tournament_result.get("total_tokens", 0) or 0),
                "artifact_size_bytes": _t_bytes,
                "outcome": "SUCCESS",
                "phase": desk.phase.value,
                "quality_score": tournament_quality,
            })
        except Exception as tournament_err:
            logger.error("[V3] %s: Tournament debate failed: %s", ticker, tournament_err, exc_info=True)
            logger.info("[V3] Falling back to classic debate agents (Bull/Bear).")
            desk.record_agent_telemetry({
                "agent_name": "v3_tournament_debate",
                "ticker": ticker,
                "elapsed_ms": int((time.monotonic() - t_tournament) * 1000),
                "loops_used": 1,
                "token_usage": 0,
                "outcome": "AGENT_ERROR",
                "phase": desk.phase.value,
                "quality_score": -1,
            })
            _queue_agent("bull_argument", bull_agent, parent="tournament_debate")
            _queue_agent("bear_rebuttal", bear_agent, parent="tournament_debate")

    async def _persist_trade_verdict():
        if desk.has_artifact("trade_decision"):
            try:
                from app.services.trade_result_saver import save_trade_result
                trade_decision = desk.trade_decision or {}

                # Contradiction gate — the shadow's first promotion. Unresolved
                # cross-desk directional dissent (e.g. board BUY over a BEARISH
                # quant/tournament verdict) is by definition mixed evidence, so
                # stated confidence is capped at 60. Deliberately NOT the full
                # downgrade-to-HOLD: only 1 of 7 flagged trades has resolved so
                # far, so the shadow keeps collecting the evidence for that.
                try:
                    from app.v3.contradiction_shadow import compute_contradiction_shadow
                    _gate = compute_contradiction_shadow(desk)
                    _conf = trade_decision.get("confidence")
                    if (
                        _gate.get("would_downgrade_to_hold")
                        and isinstance(_conf, (int, float))
                        and _conf > 60
                    ):
                        trade_decision["confidence_uncapped"] = _conf
                        trade_decision["confidence"] = 60
                        trade_decision["confidence_cap_reason"] = (
                            "contradiction_gate: unresolved cross-desk directional dissent "
                            f"({_gate.get('sentiment_by_source')})"
                        )
                        logger.warning(
                            "[V3] %s: contradiction gate capped confidence %s -> 60 (%s)",
                            ticker, _conf, _gate.get("sentiment_by_source"),
                        )
                        # 2026-07-25 audit: this cap rewrites a live decision
                        # but left its evidence only in the artifact, so the
                        # question "how often does the contradiction gate bind,
                        # and is 60 the right cap?" had no queryable answer.
                        _record_gate(
                            desk, "contradiction_confidence_cap",
                            uncapped=_conf, capped_to=60,
                            sentiment_by_source=_gate.get("sentiment_by_source"),
                        )
                except Exception as gate_err:
                    logger.warning("[V3] %s: contradiction gate failed (non-fatal): %s", ticker, gate_err)
                if not trade_decision.get("regime"):
                    trade_decision["regime"] = regime
                if not trade_decision.get("persona_used"):
                    board_decision = desk.final_decision or {}
                    trade_decision["persona_used"] = board_decision.get(
                        "persona_used", _persona_label(regime)
                    )
                # Normalize to snake_case: the LLM sometimes emits display case
                # ("Warren Buffett"), which splits persona telemetry keys.
                persona = str(trade_decision.get("persona_used") or "")
                trade_decision["persona_used"] = persona.strip().lower().replace(" ", "_")
                save_trade_result(ticker, cycle_id, trade_decision)

                # Feed the judge: llm_audit_logs + context_blobs are the
                # LLM-as-a-Judge inputs (evaluate_decision). Their producer
                # (rlm_wrapper → log_rlm_audit_trail) lost its caller in the
                # SDK migration, so decision_evaluations starved after the V2
                # era. The compressed desk context is exactly the blob whose
                # section headers the judge's faithfulness markers match.
                try:
                    from app.services.rlm_audit import log_rlm_audit_trail
                    _telemetry = desk.agent_telemetry or []
                    log_rlm_audit_trail(
                        cycle_id=cycle_id,
                        bot_id=bot_id,
                        ticker=ticker,
                        context=desk.get_compressed_context(include_debate=True),
                        trading_system_prompt="V3 pure agentic pipeline (desk-compressed context)",
                        active_model="v3_pipeline",
                        response_text=json.dumps(trade_decision, default=str),
                        tokens_used=sum(int(e.get("token_usage") or 0) for e in _telemetry),
                        execution_time=sum(int(e.get("elapsed_ms") or 0) for e in _telemetry) / 1000.0,
                        agent_step="v3_decision",
                    )
                except Exception as audit_err:
                    logger.warning("[V3] %s: decision audit log failed (non-fatal): %s", ticker, audit_err)

                # Paired challenger (observational): re-decide from the same
                # desk evidence under the experimental spec, log the pair.
                # Only runs when CHALLENGER_SPEC is set — see app/v3/challenger.
                try:
                    from app.v3.challenger import get_challenger_spec, run_challenger
                    if get_challenger_spec():
                        await run_challenger(desk, cycle_id, ticker, trade_decision)
                except Exception as ch_err:
                    logger.warning("[V3] %s: challenger failed (non-fatal): %s", ticker, ch_err)

                try:
                    from app.trading.strategy_tracker import record_strategy
                    action = trade_decision.get("action", "HOLD")
                    record_strategy(
                        strategy_candidate_id=None,
                        decision_outcome_id=None,
                        agent_prompt_hash="v3_pipeline",
                        ticker=ticker,
                        signal=action,
                        entry_price=None,
                    )
                except Exception as st_err:
                    logger.warning("[V3] %s: Strategy tracking failed (non-fatal): %s", ticker, st_err)
            except Exception as e:
                logger.error("[V3] %s: Failed to persist trade result: %s", ticker, e)

    # Subscribe live whiteboard triggers
    from app.agents.whiteboard import whiteboard
    whiteboard.subscribe(whiteboard_subscriber, ticker=ticker)

    try:
        # Run Regime Engine first to kick off the whiteboard triggers
        emit(
            "analyzing", f"v3_regime_engine_start_{ticker}",
            f"🌐 {ticker}: Running Market Regime Engine to classify global macro state...",
            status="running",
        )
        run_counts["regime_engine"] += 1

        # The regime is a property of the MARKET, not of this ticker, so it is
        # classified once per cycle and shared. Running it per ticker made the
        # same cycle contradict itself in 35 of 64 multi-ticker cycles (see
        # regime_cache). The lock is held across the LLM call so concurrent
        # tickers wait for the first answer instead of computing rivals.
        from app.v3 import regime_cache

        async with regime_cache.get_lock(cycle_id):
            cached_regime = regime_cache.get(cycle_id)
            if cached_regime is not None:
                desk.append_artifact("regime_classification", cached_regime)
                outcome = PhaseOutcome.SUCCESS
                logger.info(
                    "[V3] %s: reusing this cycle's regime classification (%s) — "
                    "not re-running the engine",
                    ticker, cached_regime.get("regime", "?"),
                )
                emit(
                    "analyzing", f"v3_regime_engine_reuse_{ticker}",
                    f"🌐 {ticker}: Regime {cached_regime.get('regime', '?')} "
                    f"(classified once for this cycle)",
                    status="ok",
                )
                # Without a telemetry row this ticker's regime node vanishes
                # from the replay flow graph and the regime→analyst edges break
                # (the same "islands" bug the tournament had). Zero elapsed is
                # accurate: no LLM call happened.
                desk.record_agent_telemetry({
                    "agent_name": "v3_regime_engine",
                    "ticker": ticker,
                    "elapsed_ms": 0,
                    "loops_used": 0,
                    "token_usage": 0,
                    "outcome": "SUCCESS",
                    "phase": desk.phase.value,
                    "quality_score": int(cached_regime.get("_quality_score", -1) or -1),
                    "reused": True,
                })
            else:
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk,
                    agent_module=regime_engine,
                    phase_name="regime_engine",
                    breaker=breaker,
                    cycle_id=cycle_id,
                    bot_id=bot_id,
                    emit=emit,
                )
                if outcome == PhaseOutcome.SUCCESS and desk.regime_classification:
                    regime_cache.put(cycle_id, desk.regime_classification)

        breaker.record_outcome("regime_engine", outcome)
        if outcome in (PhaseOutcome.TIMED_OUT,):
            whiteboard.unsubscribe(whiteboard_subscriber)
            return _build_noop_result(desk, reason="Regime engine timed out")

        if outcome == PhaseOutcome.SUCCESS and desk.regime_classification:
            await whiteboard.write_section(
                ticker=ticker,
                cycle_id=cycle_id,
                section="regime_classification",
                content=desk.regime_classification,
                author_agent="regime_engine"
            )

        # Scheduler task processing loop
        loop_counter = 0
        MAX_LOOP_ITERATIONS = 20
        # Observable topology (plan 2.4): record every scheduler iteration on
        # the desk so runaway loops can be debugged after the fact. Persisted
        # via cycle_metadata; never injected into agent prompts.
        iteration_log: list[dict] = []
        desk.cycle_metadata["pipeline_iteration_log"] = iteration_log

        while (tasks_to_run or await _has_pending_peer_requests()) and loop_counter < MAX_LOOP_ITERATIONS:
            loop_counter += 1
            await _process_peer_requests()

            if not tasks_to_run:
                break

            task = tasks_to_run.pop(0)
            name = task["name"]
            module = task["module"]
            query = task["query"]
            parent = task["parent"]

            run_counts[name] += 1
            iteration_log.append({
                "iteration": loop_counter,
                "task": name,
                "run": run_counts[name],
                "parent": parent,
                "query": (query or "")[:200],
            })
            logger.info("[V3] Executing dynamic task: %s (run %d)", name, run_counts[name])
            
            if name == "junior_analyst":
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk, agent_module=module, phase_name="junior_analyst",
                    breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    custom_instructions=query, parent_agent=parent
                )
                abort = _check_abort(desk, breaker, "junior_analyst", outcome)
                if abort:
                    whiteboard.unsubscribe(whiteboard_subscriber)
                    return abort
                # Write desk_note to whiteboard so subscriber chains FA/QA
                if outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP) and desk.desk_note:
                    await whiteboard.write_section(
                        ticker=ticker, cycle_id=cycle_id,
                        section="desk_note",
                        content=desk.desk_note,
                        author_agent="v3_junior_analyst"
                    )
                
            elif name == "fundamental_analyst":
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk, agent_module=module, phase_name="fundamental_analyst",
                    breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    custom_instructions=query, parent_agent=parent
                )
                abort = _check_abort(desk, breaker, "fundamental_analyst", outcome)
                if abort:
                    whiteboard.unsubscribe(whiteboard_subscriber)
                    return abort
                # Write fundamental_report to whiteboard so subscriber chains debate
                if outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP) and desk.fundamental_report:
                    await whiteboard.write_section(
                        ticker=ticker, cycle_id=cycle_id,
                        section="fundamental_report",
                        content=desk.fundamental_report,
                        author_agent="v3_fundamental_analyst"
                    )
                
            elif name == "quant_analyst":
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk, agent_module=module, phase_name="quant_analyst",
                    breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    custom_instructions=query, parent_agent=parent
                )
                abort = _check_abort(desk, breaker, "quant_analyst", outcome)
                if abort:
                    whiteboard.unsubscribe(whiteboard_subscriber)
                    return abort
                # Write quant_report to whiteboard so subscriber chains debate
                if outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP) and desk.quant_report:
                    await whiteboard.write_section(
                        ticker=ticker, cycle_id=cycle_id,
                        section="quant_report",
                        content=desk.quant_report,
                        author_agent="v3_quant_analyst"
                    )

            elif name == "valuation_analyst":
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk, agent_module=module, phase_name="valuation_analyst",
                    breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    custom_instructions=query, parent_agent=parent
                )
                # NO _check_abort. Every other research branch aborts the desk on
                # failure because the debate cannot proceed without it; this one
                # is non-blocking by design, so a failed valuation must leave the
                # cycle running with one fewer opinion rather than killing it.
                if outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP) and desk.valuation_report:
                    await whiteboard.write_section(
                        ticker=ticker, cycle_id=cycle_id,
                        section="valuation_report",
                        content=desk.valuation_report,
                        author_agent="v3_valuation_analyst"
                    )

            elif name == "bull_argument":
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk, agent_module=module, phase_name="bull_argument",
                    breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    custom_instructions=query, parent_agent=parent
                )
                # Deferred-item 8.2 decision (2026-07-15): a debate timeout is a
                # hard ABORT, not a silent degrade to an unmarked HOLD@0.
                abort = _check_abort(desk, breaker, "bull_argument", outcome)
                if abort:
                    whiteboard.unsubscribe(whiteboard_subscriber)
                    return abort
                # Write bull_argument to whiteboard so subscriber chains debate_judge
                if outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP) and desk.bull_argument:
                    await whiteboard.write_section(
                        ticker=ticker, cycle_id=cycle_id,
                        section="bull_argument",
                        content=desk.bull_argument,
                        author_agent="v3_bull_agent"
                    )

            elif name == "bear_rebuttal":
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk, agent_module=module, phase_name="bear_rebuttal",
                    breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    custom_instructions=query, parent_agent=parent
                )
                abort = _check_abort(desk, breaker, "bear_rebuttal", outcome)
                if abort:
                    whiteboard.unsubscribe(whiteboard_subscriber)
                    return abort
                # Write bear_rebuttal to whiteboard so subscriber chains debate_judge
                if outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP) and desk.bear_rebuttal:
                    await whiteboard.write_section(
                        ticker=ticker, cycle_id=cycle_id,
                        section="bear_rebuttal",
                        content=desk.bear_rebuttal,
                        author_agent="v3_bear_agent"
                    )

            elif name == "debate_judge":
                outcome = await _run_debate_judge(
                    desk=desk, breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    parent_agent=_SECTION_TO_AGENT.get(parent, parent),
                )
                abort = _check_abort(desk, breaker, "debate_judge", outcome)
                if abort:
                    whiteboard.unsubscribe(whiteboard_subscriber)
                    return abort
                # Write debate_judge to whiteboard so subscriber chains board_of_directors
                if outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP) and desk.debate_judge:
                    await whiteboard.write_section(
                        ticker=ticker, cycle_id=cycle_id,
                        section="debate_judge",
                        content=desk.debate_judge,
                        author_agent="v3_debate_judge"
                    )
                
            elif name == "tournament_debate":
                await _execute_tournament_debate(parent=parent)
                
            elif name == "board_of_directors":
                if desk.phase == DeskPhase.RESEARCH_DONE:
                    desk.advance_phase(DeskPhase.DEBATE_DONE)
                    save_desk(desk)
                    emit("analyzing", f"v3_debate_done_{ticker}", f"⚔️ {ticker}: Debate layer complete", status="ok")
                    
                outcome = await _run_board_of_directors(
                    desk=desk, regime=regime, breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    parent_agent=_SECTION_TO_AGENT.get(parent, parent),
                )
                # A board timeout used to leave final_decision unwritten and fall
                # through to an unmarked HOLD@0 (indistinguishable from a real
                # no-signal HOLD). Abort loudly instead (deferred item 8.2).
                abort = _check_abort(desk, breaker, "board_of_directors", outcome)
                if abort:
                    whiteboard.unsubscribe(whiteboard_subscriber)
                    return abort
                # 2026-07-25: deferred item 8.2 covered board TIMEOUTS only. Every
                # other degrade path still left final_decision null on the desk
                # while the synthesizer ran on and produced a real trade_decision —
                # 10 desks found that way, all HOLD, because the degrade path and
                # the HOLD default share one cause. `null` meant both "never ran"
                # and "ran and we lost it". Record an explicit degraded sentinel
                # instead, so the two are always distinguishable.
                if not desk.final_decision:
                    desk.append_artifact("final_decision", {
                        "summary": (
                            f"Board did not produce a decision "
                            f"(outcome={outcome.value}). No agent verdict — this is "
                            f"a recorded degrade, not a no-signal HOLD."
                        ),
                        "action": None,
                        "confidence": 0,
                        "decision_provenance": DecisionProvenance.BOARD_DEGRADED_FALLBACK.value,
                        "degrade_outcome": outcome.value,
                        "risk_flags": ["board_degraded_no_decision"],
                    })
                    save_desk(desk)
                    logger.warning(
                        "[V3] %s: board produced no final_decision (outcome=%s) — "
                        "recorded degraded sentinel", ticker, outcome.value,
                    )
                    emit("analyzing", f"v3_board_degraded_{ticker}",
                         f"⚠️ {ticker}: Board produced no decision ({outcome.value})",
                         status="warn")
                # Write final_decision to whiteboard so subscriber chains decision_synthesizer
                if outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP) and desk.final_decision:
                    await whiteboard.write_section(
                        ticker=ticker, cycle_id=cycle_id,
                        section="final_decision",
                        content=desk.final_decision,
                        author_agent="v3_board_of_directors"
                    )

            elif name == "decision_synthesizer":
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk, agent_module=module, phase_name="decision_synthesizer",
                    breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    include_debate_context=True, custom_instructions=query, parent_agent=parent
                )
                breaker.record_outcome("decision_synthesizer", outcome)
                await _persist_trade_verdict()

        if loop_counter >= MAX_LOOP_ITERATIONS:
            iteration_log.append({"iteration": loop_counter, "event": "max_loop_iterations_hit"})
            logger.warning(
                "[V3] DynamicOrchestrator hit MAX_LOOP_ITERATIONS safeguard for %s. Iteration log: %s",
                ticker,
                [f"{e.get('task', e.get('event'))}<-{e.get('parent', '')}" for e in iteration_log],
            )

    finally:
        whiteboard.unsubscribe(whiteboard_subscriber)

    # ═══════════════════════════════════════════════════════════════════
    # CONTRADICTION SHADOW — observation-only first step of the mesh.
    # Reuses the previously-dead cognition contradiction detector across the
    # finished desk and records what a "downgrade-to-HOLD on unresolved
    # dissent" gate WOULD have done — WITHOUT changing this cycle's decision.
    # Runs BEFORE save_desk so the report persists on the desk row + cycle log.
    # ═══════════════════════════════════════════════════════════════════
    try:
        from app.v3.contradiction_shadow import compute_contradiction_shadow
        _shadow = compute_contradiction_shadow(desk)
        desk.record_agent_telemetry(_shadow)
        if _shadow.get("contradiction_count"):
            emit(
                "analyzing", f"v3_shadow_{ticker}",
                f"🔀 {ticker}: Contradiction shadow — "
                f"{_shadow['contradiction_count']} cross-agent conflict(s), "
                f"would_downgrade={_shadow.get('would_downgrade_to_hold')}",
                status="ok",
                data=_shadow,
            )
    except Exception as e:
        logger.warning("[V3] %s: contradiction shadow failed (non-fatal): %s", ticker, e)

    try:
        if desk.phase == DeskPhase.INIT and desk.has_artifact("final_decision"):
            # JA triage SKIP: research/debate never ran, so INIT is the
            # correct terminal phase (same as a Triage Gate glance skip).
            logger.info("[V3] %s: JA-triage-skipped cycle — desk stays at INIT", ticker)
            save_desk(desk)
        else:
            desk.advance_phase(DeskPhase.PM_DONE)
            save_desk(desk)
    except ValueError as e:
        logger.error("[V3] %s: Pipeline failed before reaching PM_DONE. Status: %s. Error: %s", ticker, desk.phase, e)
    try:
        from app.services.memory.store import MemoryStore
        decision = desk.trade_decision or desk.final_decision or {}
        # A degraded desk has action=None; recording "None @ 0%" as an episodic
        # memory teaches the memory system a decision that was never made.
        action = (decision.get("action") or "HOLD") if not _is_degraded_decision(decision) else "DEGRADED"
        confidence = decision.get("confidence") or 0
        reasoning = decision.get("reasoning", "")
        MemoryStore().add_episodic_observation({
            "cycle_id": cycle_id,
            "ticker": ticker,
            "source_type": "v3_pipeline",
            "observation_text": (
                f"V3 cycle completed for {ticker}: {action} @ {confidence}% confidence. "
                f"Regime: {regime}. Reasoning: {reasoning[:500]}"
            ),
            "confidence_at_creation": confidence / 100.0 if confidence else 0.0,
            "outcome_label": action,
        })
        logger.info("[V3] %s: Episodic observation recorded", ticker)

        # Working-memory episodic store: read into EVERY agent prompt
        # ("Relevant Past Cycles") but its only writer was a class that was
        # never instantiated — agents saw a permanently empty section.
        try:
            from app.services.memory.episodic_memory import episodic_memory_store
            episodic_memory_store.write_episode(
                cycle_id=cycle_id,
                ticker=ticker,
                summary=f"{action} @ {confidence}% ({regime}): {reasoning[:200]}",
                key_decisions=json.dumps([action]),
                outcome="pending",
                outcome_score=0.0,
            )
        except Exception as epi_err:
            logger.warning("[V3] %s: working-memory episode write failed (non-fatal): %s", ticker, epi_err)

        # Consolidation: without this, episodic observations pile up forever
        # and canonical memories are never distilled from cycle experience —
        # the retriever would read a table nothing populates. Runs as a
        # BACKGROUND task (its output feeds future cycles, not this trade),
        # internally gated by a ≥5-unpromoted threshold and a per-ticker
        # cooldown so a failing LLM pass can't re-fire every cycle.
        from app.services.memory.consolidator import maybe_consolidate
        _task = asyncio.create_task(maybe_consolidate(ticker))
        _BG_TASKS.add(_task)
        _task.add_done_callback(_BG_TASKS.discard)
    except Exception as e:
        logger.warning("[V3] %s: Memory persistence failed (non-fatal): %s", ticker, e)

    try:
        from app.cognition.ontology.graph_sync import sync_desk_to_graph
        sync_desk_to_graph(desk, cycle_id)
    except Exception as e:
        logger.warning("[V3] %s: Brain graph sync failed (non-fatal): %s", ticker, e)

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 6: Policy Gates (Trade Execution Rules)
    # ═══════════════════════════════════════════════════════════════════
    policy_action = _apply_policy_gates(desk)

    emit(
        "analyzing", f"v3_policy_{ticker}",
        f"🛡️ {ticker}: Policy Gates evaluated → {policy_action}",
        status="ok",
    )

    # Persist the ENFORCED label on the trade row. Without this, a blocked
    # SELL/BUY is indistinguishable from an executed one in trade_results —
    # the 07-23 audit found 3 SELLs on unheld tickers with no recorded gate.
    try:
        _persist_policy_action(cycle_id, ticker, policy_action)
    except Exception as pe:
        logger.warning("[V3] %s: policy_action persist failed (non-fatal): %s", ticker, pe)
    _decided = (desk.trade_decision or desk.final_decision or {})
    if policy_action.startswith("HOLD_") and (_decided.get("action") in ("BUY", "SELL")):
        emit(
            "analyzing", f"v3_policy_blocked_{ticker}",
            f"🚫 {ticker}: {_decided.get('action')} @ {_decided.get('confidence', '?')}% "
            f"BLOCKED by policy gate → {policy_action}",
            status="warning",
        )

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

    # Inject the actual policy action so upstream callers (like cycle_main) can respect it
    result["policy_action"] = policy_action

    # Record the tier the Triage Gate actually evaluated — _build_v1_compatible_result
    # hardcodes "v3_full", which made analysis_results.triage_tier wrong for
    # every deep/standard ticker (triage analytics grouped on a constant).
    result["triage_tier"] = triage_tier

    return result

def _persist_policy_action(cycle_id: str, ticker: str, policy_action: str) -> None:
    """Record the enforced policy label on the trade row (PG + Mongo mirror)."""
    from app.db.connection import get_db

    with get_db() as db:
        db.execute(
            "UPDATE trade_results SET policy_action = %s WHERE cycle_id = %s AND ticker = %s",
            [policy_action, cycle_id, ticker],
        )
    try:
        from app.db import mongo_store
        if mongo_store.writes_mongo("trade_results"):
            mongo_store.get_doc_db()["trade_results"].update_many(
                {"cycle_id": cycle_id, "ticker": ticker},
                {"$set": {"policy_action": policy_action}},
            )
    except Exception as me:
        logger.warning("[V3] mongo policy_action mirror failed (non-fatal): %s", me)


# "Not scoreable" and "degraded" are DIFFERENT questions, and conflating them
# was a bug in the first draft of this fix. A Triage-Gate skip is not
# scoreable (no agent decided) but it is a deliberate, correct outcome —
# labelling it DEGRADED would relabel healthy skips as pipeline failures in
# the memory store, the dashboard and policy_action. Only these provenances
# mean "the pipeline tried to decide and failed".
_DEGRADED_PROVENANCE = frozenset({
    DecisionProvenance.BOARD_DEGRADED_FALLBACK.value,
    DecisionProvenance.TIMEOUT_ABORT.value,
})


def _safe_confidence(value: object) -> float:
    """A confidence that cannot be read is ZERO, never a pass.

    NaN is the one that bites: it survives a NOT NULL check and every
    comparison against it is False, so `confidence < floor` reads as "cleared
    the floor" — a low-confidence gate silently inverted by a bad float. The
    audit's own traps section names this ("sanitize NaN where values are
    consumed, not only where fetched"); the gate consumed it unsanitized.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if math.isnan(value) or math.isinf(value):
        return 0.0
    return float(value)


def _is_degraded_decision(decision: dict) -> bool:
    """True when this artifact records a FAILURE to decide.

    Two independent tells, because either alone has been wrong before: an
    explicitly degraded provenance, or a null action (the sentinel shape).
    A degraded decision must never be scored, executed, or rendered as though
    an agent chose it. Deliberate skips are NOT degrades — see
    `_DEGRADED_PROVENANCE`.
    """
    if not isinstance(decision, dict):
        return False
    prov = decision.get("decision_provenance")
    if isinstance(prov, str) and prov in _DEGRADED_PROVENANCE:
        return True
    return "action" in decision and decision.get("action") is None


def _record_gate(desk: SharedDesk, label: str, **detail: Any) -> str:
    """Record an enforcing policy-gate firing, then return its label unchanged.

    2026-07-25 audit: `record_guardrail_firing` had exactly two callers, both in
    artifact_validators — so the only guardrail with rows in the table was an
    observational SHADOW flag, while every gate that actually blocks a trade
    counted nothing. The enforcement was invisible and therefore untunable.

    Returning the label keeps each gate a one-line `return`, so counting can
    never alter control flow — the failure mode that would turn telemetry into
    a trading bug. Fail-open twice over: `record_guardrail_firing` swallows its
    own errors, and the import is wrapped too, because `_apply_policy_gates`
    runs in unit tests with no DB at all.

    Named `_record_gate`, not `_gate`: `_persist_trade_verdict` already binds a
    local `_gate` (the contradiction shadow), which would shadow this helper at
    the one other site that needs it.
    """
    try:
        from app.v3.telemetry import record_guardrail_firing

        # triage_tier separates the two callers of _apply_policy_gates (the
        # full panel and the delta re-look). Without it both tiers pool into
        # one count and "how often does the delta path block?" is unanswerable.
        record_guardrail_firing(
            label,
            ticker=desk.ticker,
            cycle_id=desk.cycle_id,
            detail={
                "gate": "policy",
                "triage_tier": desk.cycle_metadata.get("triage_tier"),
                **detail,
            },
        )
    except Exception as e:  # never let telemetry break an enforcing gate
        logger.warning("[V3] policy gate telemetry failed (non-fatal): %s", e)
    return label


def _apply_policy_gates(desk: SharedDesk) -> str:
    """Apply explicit orchestration policy gates to the final decision.

    The returned policy action is ENFORCED by pipeline_service before trade
    execution (a *_POLICY_BLOCKED_* result never trades) — it is not advisory.

    Every blocking return is routed through `_record_gate` so the firing is
    counted. `HOLD_NO_SIGNAL` and `EXECUTE_*` are deliberately NOT counted —
    see their comments below.
    """
    decision = desk.trade_decision or desk.final_decision or {}
    board = desk.final_decision or {}
    # Coerced through str() and defaulted on blank, NOT `.get(..., "HOLD")`:
    #   - the degraded sentinel writes the key PRESENT with value None, so the
    #     dict default never fires and `.upper()` raised AttributeError on
    #     exactly the desk engineered to record a degrade;
    #   - a non-string action (a float, a bool from a malformed artifact) also
    #     has no .upper(), and crashing here loses the whole ticker;
    #   - a whitespace-only action used to produce the label "EXECUTE_", which
    #     is an order authorized by unparseable input.
    # Anything unreadable resolves to HOLD: the safe direction.
    action = str(decision.get("action") or "").strip().upper() or "HOLD"
    confidence = _safe_confidence(decision.get("confidence"))

    # The tail of this function returns f"EXECUTE_{action}", so an unrecognized
    # action would interpolate straight into the label the executor reads
    # ("EXECUTE_3.14", "EXECUTE_TRUE"). Only three actions exist; anything else
    # is a malformed artifact, and the safe reading of a malformed decision is
    # that no decision was made.
    if action not in ("BUY", "SELL", "HOLD"):
        logger.warning(
            "[V3] %s: unrecognized action %r in decision artifact — gating as "
            "unparseable rather than executing it", desk.ticker,
            decision.get("action"),
        )
        return _record_gate(
            desk, "HOLD_UNPARSEABLE_ACTION", raw_action=repr(decision.get("action")),
        )

    # An explicit degrade is not a no-signal HOLD — it is the absence of a
    # decision. Both block the trade, but they must stay distinguishable
    # downstream (the dashboard label and the scorecard read this string).
    if _is_degraded_decision(decision):
        return _record_gate(
            desk, "HOLD_DEGRADED_NO_DECISION",
            provenance=decision.get("decision_provenance"),
        )

    # NOT recorded as a guardrail firing, deliberately: a genuine no-signal HOLD
    # is a normal decision the desk is entitled to reach, not a safety gate
    # rewriting one. Counting it would swamp the table with routine outcomes and
    # make the real firing rate unreadable. Do not "fix" this omission.
    if action == "HOLD":
        return "HOLD_NO_SIGNAL"

    # A SELL is only executable for a position the bot actually holds — there is
    # no shorting. The holdings flag is resolved once at desk-build time
    # (_build_cycle_metadata → cycle_metadata["held"]). This gate is the label
    # the dashboard shows and pipeline_service enforces, so it MUST express
    # "can't sell, not held" itself — historically it fell through to
    # EXECUTE_SELL and the executor dropped the order silently, showing
    # "EXECUTE_SELL, 0 orders, no reason". Block only on an AFFIRMATIVE not-held
    # (held is False); if holdings are unknown (None — e.g. the context fetch
    # raised at build time) fall through and let the executor's own position
    # check + paper_trader guard remain the backstop.
    if action == "SELL":
        held = desk.cycle_metadata.get("held")
        if held is None:
            # Holdings unknown (context fetch failed at build time) — resolve
            # live instead of falling through: the fallthrough is how three
            # unheld SELLs reached the executor as silent no-ops (07-23 audit).
            try:
                from app.trading.paper_trader import get_portfolio
                portfolio = get_portfolio(desk.cycle_metadata.get("bot_id") or "")
                held = any(
                    (p.get("ticker") or "").upper() == desk.ticker.upper()
                    for p in (portfolio.get("positions") or [])
                )
            except Exception:
                held = None  # truly unknown — executor guard stays the backstop
        if held is False:
            return _record_gate(desk, "HOLD_NO_POSITION", action=action)

    # Dynamic confidence floor (plan 3.1): the board may RAISE the bar for
    # this specific decision, never lower the firm-wide threshold.
    #
    # pipeline_service:1376 enforces the base threshold a SECOND time. That
    # looks like a redundant carrier and was a candidate for collapsing in the
    # 2026-07-29 simplification pass. It was measured and KEPT: the second check
    # sits in an `elif` after the policy-gate branch, so it only runs when this
    # function did not produce a policy_action at all — and 5 of 35 executable
    # decisions since 07-23 arrived with policy_action NULL (all on 07-23
    # itself, while the column was being deployed). Two of those five were
    # sub-floor BUYs (GOOG 64, C 60) that only the second check would have
    # caught. It is a fail-safe for paths that skip these gates, not a duplicate
    # answer to the same question. Do not remove it without re-running that
    # query and finding zero.
    from app.services.parameter_store import get_param as _get_param
    floor = _get_param("ANALYSIS_CONFIDENCE_THRESHOLD")
    board_floor = board.get("confidence_floor")
    if isinstance(board_floor, (int, float)) and not isinstance(board_floor, bool):
        floor = max(floor, board_floor)
    if confidence < floor:
        return _record_gate(
            desk, "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE",
            action=action, confidence=confidence, floor=floor,
            board_floor=board_floor,
        )

    if not desk.has_artifact("regime_classification"):
        return _record_gate(desk, "HOLD_POLICY_BLOCKED_MISSING_REGIME", action=action)

    # Stop/target sanity against the last close (2026-07-28 fidelity audit).
    # `stop_loss`, `take_profit` and `position_size_pct` are emitted by BOTH the
    # Board and the synthesizer and NOTHING checked any of them — they were the
    # largest unguarded surface left, and they size real orders.
    #
    # Measured over 14 days: 3 of 358 decisions carried an implausible level,
    # including LMT with a stop of $0.92 and a target of $1.25 against a $581
    # close. That is not a bad trade, it is a decimal error, and executing it
    # would either never stop out or liquidate instantly.
    #
    # The band is deliberately wide (0.3x-1.5x of close for a stop, 0.7x-3.0x
    # for a target). It is a DECIMAL-ERROR detector, not a strategy opinion: a
    # tight stop and an ambitious target are both legitimate and must pass.
    _levels = {
        "stop_loss": (0.3, 1.5),
        "take_profit": (0.7, 3.0),
    }
    _last_close = None
    try:
        from app.db.connection import get_db

        with get_db() as _db:
            _row = _db.execute(
                "SELECT close FROM price_history WHERE ticker = %s "
                "ORDER BY date DESC LIMIT 1", [desk.ticker],
            ).fetchone()
        _last_close = float(_row[0]) if _row and _row[0] else None
    except Exception as _e:  # noqa: BLE001 — a price lookup must never block
        logger.debug("[V3] %s: stop/target sanity lookup failed: %s",
                     desk.ticker, _e)

    if _last_close and _last_close > 0:
        for _field, (_lo, _hi) in _levels.items():
            _v = decision.get(_field)
            if not isinstance(_v, (int, float)) or isinstance(_v, bool):
                continue
            if _v <= 0 or not (_last_close * _lo <= _v <= _last_close * _hi):
                # Dropped, not clamped. A clamped level is a number the desk
                # never chose, presented as though it had — and the Board's
                # exit logic would then act on our arithmetic, not its thesis.
                decision[_field] = None
                _record_gate(
                    desk, "DROPPED_IMPLAUSIBLE_LEVEL", action=action,
                    field=_field, value=_v, last_close=round(_last_close, 2),
                )
                logger.warning(
                    "[V3] %s: %s=%s is implausible against a %.2f close — "
                    "dropped (band %.1fx-%.1fx)",
                    desk.ticker, _field, _v, _last_close, _lo, _hi,
                )

    # Strategy health (Ruuj ch.5): "has the model degraded" is checked
    # separately from "is it losing money". A decision-critical agent whose
    # telemetry quality collapses must not keep OPENING positions — SELLs
    # stay allowed (a degraded model should still be able to de-risk).
    # Fails open inside get_pipeline_health; belt-and-braces here too.
    if action == "BUY":
        try:
            from app.quant.strategy_health import get_pipeline_health
            health = get_pipeline_health()
            if health.get("status") == "CUT":
                logger.warning(
                    "[V3] %s: BUY blocked — strategy health CUT (driver=%s: %s)",
                    desk.ticker, health.get("driver"), health.get("reason"),
                )
                return _record_gate(
                    desk, "HOLD_POLICY_BLOCKED_DEGRADED_MODEL",
                    driver=health.get("driver"), reason=health.get("reason"),
                )
        except Exception as health_err:
            logger.warning("[V3] %s: strategy health check failed (fail-open): %s", desk.ticker, health_err)

    # Conviction sub-scores (plan 3.2): a board that admits its data quality
    # is poor gets blocked regardless of headline confidence.
    conviction = board.get("conviction_vector") or {}
    data_quality = conviction.get("data_quality") if isinstance(conviction, dict) else None
    if isinstance(data_quality, (int, float)) and not isinstance(data_quality, bool) and data_quality < _get_param("DATA_QUALITY_FLOOR"):
        return _record_gate(
            desk, "HOLD_POLICY_BLOCKED_DATA_QUALITY",
            action=action, data_quality=data_quality,
        )

    tournament = getattr(desk, "tournament_result", None) or {}

    # Jury-majority veto is binding by default. The board may override it
    # ONLY with an explicit written justification (plan 3.3) — the veto then
    # degrades to a standing risk flag, which still demands full mitigation.
    veto_overridden = False
    if tournament.get("vetoed"):
        justification = str(board.get("override_justification") or "").strip()
        if board.get("overrides_veto") and justification:
            veto_overridden = True
            logger.warning(
                "[V3] %s: Board overrides jury-majority veto — justification: %s",
                desk.ticker, justification[:300],
            )
        else:
            return _record_gate(desk, "HOLD_POLICY_BLOCKED_JURY_VETO", action=action)

    # A solo juror veto is a standing risk flag: the board may trade through
    # it ONLY with explicit mitigation — a defined stop-loss, a dynamic
    # trigger, and its own reasoned position size. Anything less holds.
    # An overridden jury veto is held to the same standard.
    if tournament.get("risk_flags") or veto_overridden:
        mitigation = {**(desk.final_decision or {}), **(desk.trade_decision or {})}
        has_stop = isinstance(mitigation.get("stop_loss"), (int, float))
        has_trigger = bool(mitigation.get("dynamic_trigger"))
        has_size = isinstance(mitigation.get("position_size_pct"), (int, float))
        if not (has_stop and has_trigger and has_size):
            return _record_gate(
                desk, "HOLD_POLICY_BLOCKED_UNMITIGATED_RISK",
                action=action, has_stop=has_stop,
                has_trigger=has_trigger, has_size=has_size,
                veto_overridden=veto_overridden,
            )

    # LAST gate before execution, deliberately. No price history at all is not
    # a stale-data judgement call — it is the absence of the input every
    # technical claim in the artifact rests on. 2026-07-26
    # (cycle-v3-1785107795): ASIC and ARCVF had ZERO price_history rows, yet
    # both ran the full panel and ASIC reached BUY at 68 confidence. Its own
    # risk_flags said "Missing technical indicators (RSI/SMA)" and the number
    # did not move — the model can SEE the hole and still price around it.
    # Only the confidence floor stopped that order, by luck of the number.
    #
    # Categorical, so a gate is the right instrument: "zero rows" has no
    # tunable threshold to get wrong, unlike staleness (graded, and it rides
    # in the prompt instead — see build_technical_baseline_block).
    #
    # Ordered LAST rather than early because the probe needs a database, and a
    # DB that is absent or empty answers "no rows" for every ticker. Placed up
    # front it out-ranked every specific gate — a degraded-model block, a jury
    # veto, an unheld SELL would all have been relabelled HOLD_NO_PRICE_DATA,
    # which is both a worse diagnosis and a silent loss of the real reason.
    # Here it can only convert a would-be EXECUTE into a block, never mask
    # another gate's verdict.
    #
    # Fails OPEN on a probe error: this catches a missing ticker, not a
    # Postgres hiccup.
    try:
        from app.quant.technical_baseline import has_price_history

        if not has_price_history(desk.ticker):
            return _record_gate(
                desk, "HOLD_NO_PRICE_DATA", action=action, confidence=confidence,
            )
    except Exception as ph_err:  # noqa: BLE001 — never block on a probe failure
        logger.warning(
            "[V3] %s: price-history probe failed (fail-open): %s",
            desk.ticker, ph_err,
        )

    # NOT recorded: reaching here means no guardrail fired. Counting the clean
    # path would make the table a decision log rather than a firing log.
    return f"EXECUTE_{action}"



# ═══════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════


def _check_abort(
    desk: SharedDesk,
    breaker: CircuitBreaker,
    phase_name: str,
    outcome: PhaseOutcome,
) -> dict[str, Any] | None:
    """Check if a phase outcome should abort the pipeline.

    Returns a noop result dict if aborting, or None if the pipeline should continue.
    This deduplicates the 6-line abort-check pattern repeated across research topologies.
    """
    ticker = desk.ticker

    if outcome in (PhaseOutcome.TIMED_OUT,):
        logger.error("[V3] %s: %s TIMED OUT — aborting pipeline", ticker, phase_name)
        desk.advance_phase(DeskPhase.ABORTED, outcome)
        save_desk(desk)
        return _build_noop_result(desk, reason=f"{phase_name} timed out")

    if breaker.should_abort(phase_name, outcome):
        logger.error("[V3] %s: Circuit breaker tripped on %s — aborting pipeline", ticker, phase_name)
        desk.advance_phase(DeskPhase.ABORTED, outcome)
        save_desk(desk)
        return _build_noop_result(desk, reason=breaker.get_abort_reason(phase_name))

    breaker.record_outcome(phase_name, outcome)
    return None


async def _run_agent_with_circuit_breaker(
    desk: SharedDesk,
    agent_module: Any,
    phase_name: str,
    breaker: CircuitBreaker,
    cycle_id: str,
    bot_id: str,
    emit: Any,
    include_debate_context: bool = False,
    custom_instructions: str = "",
    parent_agent: str = "",
) -> PhaseOutcome:
    """Run an agent with circuit breaker retry logic.

    On first failure (TOOL_OUTAGE or AGENT_ERROR), retries once.
    On second failure, returns the failure outcome for the orchestrator
    to decide whether to abort or continue.
    """
    from app.config import settings
    timeout = float(settings.ANALYSIS_WORKER_TIMEOUT_SECONDS)

    async with concurrency_controller.track(label="v3_agent"):
        outcome = await run_v3_agent(
            desk=desk,
            agent_module=agent_module,
            cycle_id=cycle_id,
            bot_id=bot_id,
            emit=emit,
            include_debate_context=include_debate_context,
            timeout_seconds=timeout,
            custom_instructions=custom_instructions,
            parent_agent=parent_agent,
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
                    timeout_seconds=timeout,
                    custom_instructions=custom_instructions,
                    parent_agent=parent_agent,
                    is_retry=True,
                )

    return outcome


# Queued parents are whiteboard *section* names; office-graph edges key on
# *agent* node ids. Normalize before emitting so edges actually connect.
_SECTION_TO_AGENT = {
    "regime_classification": "regime_engine",
    "desk_note": "junior_analyst",
    "fundamental_report": "fundamental_analyst",
    "quant_report": "quant_analyst",
    "valuation_report": "valuation_analyst",
    "bull_argument": "bull_agent",
    "bear_rebuttal": "bear_agent",
    "tournament_result": "tournament_debate",
    "final_decision": "board_of_directors",
}


async def _run_debate_judge(
    desk: SharedDesk,
    breaker: CircuitBreaker,
    cycle_id: str,
    bot_id: str,
    emit: Any,
    parent_agent: str = "",
) -> PhaseOutcome:
    """Run the Debate Judge to synthesize parallel Bull and Bear arguments."""
    from app.v3.agents import debate_judge

    return await _run_agent_with_circuit_breaker(
        desk=desk,
        agent_module=debate_judge,
        phase_name="debate_judge",
        breaker=breaker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        emit=emit,
        include_debate_context=True,
        parent_agent=parent_agent,
    )


async def _run_board_of_directors(
    desk: SharedDesk,
    regime: str,
    breaker: CircuitBreaker,
    cycle_id: str,
    bot_id: str,
    emit: Any,
    parent_agent: str = "",
) -> PhaseOutcome:
    """Run the Board of Directors with a regime-swapped persona.

    The system prompt is hot-swapped based on the Market Regime Engine's
    classification:
    - HIGH_VOLATILITY → Jim Simons (pure quant)
    - DEEP_DISCOUNT → Warren Buffett (pure fundamentals)
    - CONTRADICTORY → Jane Street (find mispricings)
    """
    import types
    from app.v3.agents.board_of_directors import (
        get_persona_prompt, AGENT_NAME, ARTIFACT_TYPE, TOOL_WHITELIST,
    )

    persona_prompt = get_persona_prompt(regime)

    bod_module = types.ModuleType("board_of_directors_module")
    bod_module.AGENT_NAME = AGENT_NAME
    bod_module.TOOL_WHITELIST = list(TOOL_WHITELIST)
    bod_module.ARTIFACT_TYPE = ARTIFACT_TYPE
    bod_module.SYSTEM_PROMPT = persona_prompt

    emit(
        "analyzing", f"v3_bod_{desk.ticker}",
        f"🎯 {desk.ticker}: Board of Directors convening "
        f"(regime: {regime}, persona: {_persona_label(regime)})",
        status="running",
        data={
            "kind": "board_convened",
            "ticker": desk.ticker,
            "persona": _persona_label(regime),
            "regime": regime,
        },
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
        parent_agent=parent_agent,
    )


def _settings_no_trade_gate_enabled() -> bool:
    """Kill switch for the no-trade-available gate. Defaults ON; set
    V3_NO_TRADE_GATE=false to restore the always-debate behavior."""
    try:
        from app.config import settings as _s
        return bool(getattr(_s, "V3_NO_TRADE_GATE", True))
    except Exception:  # noqa: BLE001 — a config miss must not disable the gate
        return True


def _research_unanimously_bearish(desk) -> bool:
    """True only when EVERY research desk that reported came back BEARISH.

    Deliberately strict. It requires at least two opinions and treats a missing
    or unparseable stance as "not bearish", so a failed analyst can never
    manufacture unanimity — the gate should under-fire rather than skip a
    debate that had something to say.
    """
    stances = []
    for key in ("fundamental_report", "quant_report", "desk_note"):
        artifact = getattr(desk, key, None) or {}
        if not isinstance(artifact, dict):
            continue
        raw = artifact.get("thesis_direction")
        # The junior's desk_note has no thesis_direction; its catalyst_call
        # carries the equivalent claim.
        if raw is None and isinstance(artifact.get("catalyst_call"), dict):
            raw = artifact["catalyst_call"].get("direction")
        if raw is None:
            continue
        stances.append(str(raw).strip().upper())

    if len(stances) < 2:
        return False
    return all(s == "BEARISH" for s in stances)


def _format_macro_briefing(snapshot: dict) -> str:
    """Format get_latest_market_snapshot() into a compact macro briefing.

    Returns "" for an empty/missing snapshot so nothing is injected.
    """
    if not snapshot or not isinstance(snapshot, dict):
        return ""

    # Friendly labels for the key instruments; sector ETFs appended below.
    labels = [
        ("VIX", "VIX (volatility)"),
        ("VIX3M", "VIX 3-Month"),
        ("GSPC", "S&P 500 (SPX)"),
        ("IXIC", "Nasdaq Composite"),
        ("RUT", "Russell 2000"),
        ("DJI", "Dow Jones"),
        ("TNX", "10-Year Yield"),
        ("FVX", "5-Year Yield"),
        ("IRX", "13-Week T-Bill"),
        ("TYX", "30-Year Yield"),
        ("DX", "US Dollar (DXY)"),
    ]
    lines = []
    as_of = ""
    for sym, label in labels:
        entry = snapshot.get(sym)
        if isinstance(entry, dict) and entry.get("close") is not None:
            try:
                lines.append(f"- {label}: {float(entry['close']):.2f}")
            except (TypeError, ValueError):
                continue
            as_of = as_of or str(entry.get("date", ""))

    if not lines:
        return ""

    # Sector ETFs: the snapshot carries XLK/XLF/... but they were silently
    # dropped, so the regime engine judged sector_momentum/rotation with zero
    # sector data (and made no tool calls to compensate).
    try:
        from app.collectors.market_regime_collector import ETF_TO_SECTOR
        sector_lines = []
        for etf, sector in ETF_TO_SECTOR.items():
            entry = snapshot.get(etf)
            if isinstance(entry, dict) and entry.get("close") is not None:
                try:
                    sector_lines.append(f"- {sector} ({etf}): {float(entry['close']):.2f}")
                except (TypeError, ValueError):
                    continue
        if sector_lines:
            lines.append("Sector ETFs (close):")
            lines.extend(sector_lines)
    except Exception:
        pass

    # FRED yield-curve + credit-stress lines (2026-07-21 audit): the regime
    # engine averages 1.1 loops, so factor inputs must arrive IN the briefing —
    # it demonstrably won't fetch them. Data already lives in macro_indicators.
    try:
        from app.services.retrieval_context import fred_curve_credit_lines
        lines.extend(fred_curve_credit_lines())
    except Exception:
        pass

    # SPY put/call + upcoming high-impact US events (2026-07-23 collector
    # wave) — same rationale: positioning/eventrisk must arrive in-briefing.
    try:
        from app.v3.alt_data_block import alt_macro_lines
        lines.extend(alt_macro_lines())
    except Exception:
        pass

    # Computed trend/breadth (2026-07-24 audit): every line above is a LEVEL.
    # trend_strength, sector_momentum and liquidity are slope/breadth questions
    # that a list of levels cannot answer — and the engine made 0 tool calls in
    # 366 runs while still scoring trend_strength 0.81 on average. These lines
    # are the measured slopes those factors are supposed to read. Kept LAST so
    # the header covers only what it actually computed.
    try:
        from app.v3.macro_trend import build_macro_trend_lines
        trend_lines = build_macro_trend_lines()
        if trend_lines:
            lines.append("Computed trend (measured from daily closes, not estimated):")
            lines.extend(trend_lines)
    except Exception:
        pass

    header = f"Latest close values{f' (as of {as_of})' if as_of else ''}:"
    return header + "\n" + "\n".join(lines)


def _regime_recommends_skip_fa(content: dict) -> bool:
    """Should the Fundamental Analyst be skipped this cycle?

    The Regime Engine owns this decision via suggested_pipeline_modifications
    (plan 1.3). Artifacts without the field (older prompt, partial output)
    fall back to the legacy HIGH_VOLATILITY label heuristic.
    """
    mods = content.get("suggested_pipeline_modifications")
    if isinstance(mods, list):
        return "skip_fundamental_analyst" in mods or "skip_fa" in mods
    return content.get("regime") == "HIGH_VOLATILITY"


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

    # Fetch position context — pushed for BOTH held and not-held. Without the
    # explicit not-held line, agents had no pushed signal and could reason
    # their way into an EXECUTE_SELL on a ticker the bot doesn't hold (a
    # guaranteed-dead trade attempt at the paper trader).
    try:
        from app.tools.portfolio_tools import get_position_context
        pos_ctx = get_position_context(ticker, bot_id)
        if pos_ctx and pos_ctx.get("held"):
            metadata["portfolio_context"] = (
                f"CURRENTLY HOLDING {ticker}: "
                f"Entry ${(pos_ctx.get('avg_entry') or 0):.2f}, "
                f"P&L {(pos_ctx.get('unrealized_pnl_pct') or 0):+.1f}%, "
                f"Held {pos_ctx.get('holding_days', 0)} days."
            )
            metadata["held"] = True
        else:
            metadata["portfolio_context"] = (
                f"NO OPEN POSITION in {ticker}. The bot cannot SELL what it "
                "does not hold (no shorting) — a SELL decision is only valid "
                "for held tickers."
            )
            metadata["held"] = False
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
    # Never let a degraded desk hand downstream a plausible "HOLD": the whole
    # point of the sentinel is that no agent decided. HOLD blocks the trade the
    # same way, but it is a claim; DEGRADED is the absence of one.
    if _is_degraded_decision(decision):
        action = "DEGRADED"
    else:
        action = decision.get("action") or "HOLD"
    confidence = decision.get("confidence") or 0

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
    dynamic_trigger = decision.get("dynamic_trigger")
    exit_style = decision.get("exit_style")
    # Sizing is situational: the board reasons about position_size_pct; the
    # synthesizer may override it. Execution honors this over any formula.
    _merged = {**(desk.final_decision or {}), **(desk.trade_decision or {})}
    position_size_pct = _merged.get("position_size_pct")

    # Consensus + data-quality feed the code-side sizing haircut in
    # pipeline_service.resolve_buy_size_pct (2026-07-21: formulas moved out of
    # the synthesizer prompt into code, where arithmetic is reliable).
    internal_consensus = _merged.get("internal_consensus_score")
    _conviction = (desk.final_decision or {}).get("conviction_vector") or {}
    data_quality = _conviction.get("data_quality") if isinstance(_conviction, dict) else None

    # Token sum from telemetry
    total_tokens = sum(
        entry.get("token_usage", 0) for entry in desk.agent_telemetry
    )

    # Institutional conviction data (non-fatal — gracefully degrade)
    institutional_conviction = {}
    try:
        from app.collectors.fund_scanner import get_institutional_signal
        inst = get_institutional_signal(desk.ticker)
        institutional_conviction = {
            "fund_count": inst["fund_count"],
            "total_value": inst["total_institutional_value"],
            "has_top_performer": inst["has_top_performer"],
            "top_performer_names": inst["top_performer_names"],
            "momentum": inst["momentum"],
            "has_new_position": inst["has_new_position"],
        }
    except Exception:
        pass

    # Build v2_metadata for backward compatibility with the frontend's debate view
    v2_debate = {
        "judge_action": action,
        "judge_confidence": confidence,
        "winning_side": "split",
        "integrity_status": "passed",
        "transcript": ""
    }

    if desk.tournament_result:
        tr = desk.tournament_result
        v2_debate["winning_side"] = tr.get("winning_side", "split")
        v2_debate["judge_action"] = tr.get("action", action)
        v2_debate["judge_confidence"] = tr.get("confidence", confidence)
        v2_debate["integrity_status"] = "vetoed" if tr.get("vetoed") else "passed"
        
        transcript_parts = []
        transcript_parts.append(f"🏆 TOURNAMENT DEBATE SUMMARY:\n{tr.get('summary', '')}\n")
        transcript_parts.append("📐 PITCHES GENERATED:")
        for p in tr.get("pitches", []):
            transcript_parts.append(f"  • {p.get('persona', '?')}: {p.get('claim', '')} (Equation: {p.get('equation', '')})")
        transcript_parts.append("\n🛡️ BACKTEST SURVIVORS:")
        for s in tr.get("survivors", []):
            transcript_parts.append(f"  • {s.get('persona', '?')}: {s.get('claim', '')} (Backtest PnL: {(s.get('backtest_pnl') or 0):.2f}%)")
        jury = tr.get("jury_verdict", {})
        if jury:
            transcript_parts.append(f"\n⚖️ JURY VERDICT: Average Score: {jury.get('average_score', 5.0)}/10 | Vetoed: {jury.get('vetoed', False)}")
        v2_debate["transcript"] = "\n".join(transcript_parts)
    else:
        # Classic debate fallbacks
        d_res = _extract_debate_result(desk)
        if d_res:
            v2_debate["winning_side"] = d_res.get("winning_side", "tie")
            v2_debate["judge_action"] = d_res.get("action", action)
            v2_debate["judge_confidence"] = d_res.get("confidence", confidence)
            
            transcript_parts = []
            if desk.bull_argument:
                transcript_parts.append(f"🟢 BULL THESIS (Confidence: {desk.bull_argument.get('confidence', 0)}%):\n{desk.bull_argument.get('summary', '')}\n")
            if desk.bear_rebuttal:
                transcript_parts.append(f"🔴 BEAR REBUTTAL (Confidence: {desk.bear_rebuttal.get('confidence', 0)}%):\n{desk.bear_rebuttal.get('summary', '')}\n")
            if desk.debate_judge:
                transcript_parts.append(f"⚖️ JUDGE VERDICT (Confidence: {desk.debate_judge.get('confidence', 0)}%):\n{desk.debate_judge.get('summary', '')}\n")
            v2_debate["transcript"] = "\n".join(transcript_parts)

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
            "take_profit": take_profit,
            "dynamic_trigger": dynamic_trigger,
            "position_size_pct": position_size_pct,
            "exit_style": exit_style,
            "internal_consensus_score": internal_consensus,
            "data_quality": data_quality,
        },
        "c_result": {
            "action": action,
            "confidence": int(confidence),
            "rationale": rationale,
        },
        "d_result": _extract_debate_result(desk),
        "institutional_conviction": institutional_conviction,
        "human_review": False,
        "agent_tokens": total_tokens,
        "rlm_tokens": 0,
        "total_tokens": total_tokens,
        "total_time_s": round(elapsed_s, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "v2_metadata": {
            "debate": v2_debate,
            "stages_completed": ["regime_classification", "research", "debate", "decision"],
        },
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

    # Build a lookup from agent telemetry for token counts
    token_lookup: dict[str, int] = {}
    for entry in desk.agent_telemetry:
        name = entry.get("agent_name", "")
        tokens = entry.get("token_usage", 0)
        if name and tokens:
            token_lookup[name] = token_lookup.get(name, 0) + tokens

    if desk.desk_note:
        results["junior_analyst"] = {
            "response": desk.desk_note.get("summary", ""),
            "tokens": token_lookup.get("v3_junior_analyst", 0)
        }
    if desk.fundamental_report:
        results["fundamental_analyst"] = {
            "response": desk.fundamental_report.get("summary", ""),
            "tokens": token_lookup.get("v3_fundamental_analyst", 0)
        }
    if desk.quant_report:
        results["quant_analyst"] = {
            "response": desk.quant_report.get("summary", ""),
            "tokens": token_lookup.get("v3_quant_analyst", 0)
        }

    # IMPORTANT: Save telemetry and quality scores to DB
    persist_telemetry(desk)

    return results


def _extract_debate_result(desk: SharedDesk) -> dict[str, Any] | None:
    """Extract debate result from SharedDesk for V1 compatibility."""

    def _safe_int(val, default=0):
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    # Tournament mode (the default): bull_argument/bear_rebuttal are never set,
    # so derive the debate result from the tournament artifact instead.
    tournament = getattr(desk, "tournament_result", None)
    if tournament:
        vetoed = bool(tournament.get("vetoed"))
        return {
            "action": tournament.get("action", "HOLD"),
            "confidence": _safe_int(tournament.get("confidence", 0)),
            "winning_side": tournament.get("winning_side", "split"),
            "bull_confidence": 0,
            "bear_confidence": 0,
            "defense_confidence": _safe_int(tournament.get("confidence", 0)),
            "original_thesis_status": "VETOED" if vetoed else "HELD",
        }

    if not desk.bull_argument and not desk.bear_rebuttal:
        return None

    bull_conf = _safe_int((desk.bull_argument or {}).get("confidence", 0))
    bear_conf = _safe_int((desk.bear_rebuttal or {}).get("confidence", 0))

    if desk.debate_judge:
        winner = desk.debate_judge.get("winner", "tie")
        conf = _safe_int(desk.debate_judge.get("final_confidence", 0))
        judge_action = "BUY" if winner == "bull" else ("SELL" if winner == "bear" else "HOLD")
        summary = desk.debate_judge.get("summary", "")
    else:
        winner = "tie"
        conf = 0
        judge_action = "HOLD"
        summary = "Debate judge failed."

    return {
        "action": judge_action,
        "confidence": conf,
        "winning_side": winner,
        "bull_confidence": bull_conf,
        "bear_confidence": bear_conf,
        "defense_confidence": conf,
        "original_thesis_status": "HELD" if winner in ("bull", "tie") else "BROKEN",
    }
