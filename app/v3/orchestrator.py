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
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from app.v3.shared_desk import SharedDesk, DeskPhase, PhaseOutcome
from app.v3.guardrails import CircuitBreaker
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
                "persona_used": "Triage Gate"
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
                desk.append_artifact("final_decision", {
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
                })
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
        nonlocal debate_dispatched
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
            facts = []
            for artifact_name, fact_type in (
                ("fundamental_report", "fundamental_report"),
                ("quant_report", "technical_quant_report"),
                ("desk_note", "news_sentiment_desk_note"),
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
                "vetoed": tournament_result.get("jury_verdict", {}).get("vetoed", False),
                "risk_flags": tournament_result.get("risk_flags", []),
                "total_tokens": tournament_result.get("total_tokens", 0),
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

            desk.record_agent_telemetry({
                "agent_name": "v3_tournament_debate",
                "ticker": ticker,
                "elapsed_ms": int((time.monotonic() - t_tournament) * 1000),
                "loops_used": 1,
                "token_usage": int(tournament_result.get("total_tokens", 0) or 0),
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
        action = decision.get("action", "HOLD")
        confidence = decision.get("confidence", 0)
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

def _apply_policy_gates(desk: SharedDesk) -> str:
    """Apply explicit orchestration policy gates to the final decision.

    The returned policy action is ENFORCED by pipeline_service before trade
    execution (a *_POLICY_BLOCKED_* result never trades) — it is not advisory.
    """
    decision = desk.trade_decision or desk.final_decision or {}
    board = desk.final_decision or {}
    action = decision.get("action", "HOLD").upper()
    confidence = decision.get("confidence", 0)

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
    if action == "SELL" and desk.cycle_metadata.get("held") is False:
        return "HOLD_NO_POSITION"

    # Dynamic confidence floor (plan 3.1): the board may RAISE the bar for
    # this specific decision, never lower the firm-wide threshold.
    # pipeline_service still enforces the base threshold as belt-and-braces.
    from app.services.parameter_store import get_param as _get_param
    floor = _get_param("ANALYSIS_CONFIDENCE_THRESHOLD")
    board_floor = board.get("confidence_floor")
    if isinstance(board_floor, (int, float)) and not isinstance(board_floor, bool):
        floor = max(floor, board_floor)
    if confidence < floor:
        return "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"

    if not desk.has_artifact("regime_classification"):
        return "HOLD_POLICY_BLOCKED_MISSING_REGIME"

    # Conviction sub-scores (plan 3.2): a board that admits its data quality
    # is poor gets blocked regardless of headline confidence.
    conviction = board.get("conviction_vector") or {}
    data_quality = conviction.get("data_quality") if isinstance(conviction, dict) else None
    if isinstance(data_quality, (int, float)) and not isinstance(data_quality, bool) and data_quality < _get_param("DATA_QUALITY_FLOOR"):
        return "HOLD_POLICY_BLOCKED_DATA_QUALITY"

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
            return "HOLD_POLICY_BLOCKED_JURY_VETO"

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
            return "HOLD_POLICY_BLOCKED_UNMITIGATED_RISK"

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
                )

    return outcome


# Queued parents are whiteboard *section* names; office-graph edges key on
# *agent* node ids. Normalize before emitting so edges actually connect.
_SECTION_TO_AGENT = {
    "regime_classification": "regime_engine",
    "desk_note": "junior_analyst",
    "fundamental_report": "fundamental_analyst",
    "quant_report": "quant_analyst",
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
    dynamic_trigger = decision.get("dynamic_trigger")
    exit_style = decision.get("exit_style")
    # Sizing is situational: the board reasons about position_size_pct; the
    # synthesizer may override it. Execution honors this over any formula.
    _merged = {**(desk.final_decision or {}), **(desk.trade_decision or {})}
    position_size_pct = _merged.get("position_size_pct")

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
