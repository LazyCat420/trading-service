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
from app.services.adaptive_concurrency import concurrency_controller
from app.v3.telemetry import persist_telemetry
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

    desk.cycle_metadata["agent_locale"] = agent_locale
    desk.cycle_metadata["prism_overrides"] = prism_overrides or {}
    
    # Store the pre-collected report
    desk.cycle_metadata["data_report"] = data_report

    # Retrieve past cycle memory for this ticker (non-fatal)
    try:
        from app.services.memory.retriever import MemoryRetriever
        retrieval_results = MemoryRetriever.retrieve(ticker=ticker)
        if retrieval_results:
            memory_brief = MemoryRetriever.build_memory_brief(retrieval_results)
            brief_text = memory_brief.get("brief_text", "")
            if brief_text:
                desk.cycle_metadata["memory_context"] = brief_text
                logger.info(
                    "[V3] %s: Injected %d memory entries (%d chars)",
                    ticker, len(retrieval_results), len(brief_text),
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
            prev_context = previous_desk.get_compressed_context(include_debate=True)
            if prev_context and prev_context != "No artifacts on desk yet.":
                desk.cycle_metadata["previous_desk_context"] = prev_context
                
                # Calculate days old for logging
                dt_str = previous_desk.created_at
                days_old = -1
                if dt_str.endswith("Z"):
                    dt_str = dt_str[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(dt_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    days_old = (datetime.now(timezone.utc) - dt).days
                except ValueError:
                    pass
                
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
                dt_str = previous_desk.created_at
                if dt_str.endswith("Z"): dt_str = dt_str[:-1] + "+00:00"
                dt = datetime.fromisoformat(dt_str)
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                hours_old = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            except Exception as e:
                logger.warning("[V3] %s: Triage hours_old calculation failed (defaulting to 9999): %s", ticker, e)

        if hours_old >= settings.TRIAGE_DEEP_HOURS or news_count >= settings.TRIAGE_DEEP_NEWS_VOLUME:
            triage_tier = "v3_deep"
        elif hours_old <= settings.TRIAGE_GLANCE_HOURS and news_count < settings.TRIAGE_DEEP_NEWS_VOLUME:
            triage_tier = "v3_glance"
        else:
            triage_tier = "v3_standard"

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
        nonlocal regime
        sec = event.get("section")
        auth = event.get("author")
        logger.info("[V3] Whiteboard event trigger: section '%s' updated by '%s'", sec, auth)
        
        if sec == "regime_classification":
            content = event.get("content") or {}
            regime = content.get("regime", "CONTRADICTORY")
            
            if regime == "HIGH_VOLATILITY":
                logger.info("[V3] High Volatility regime detected. Running JA & QA. Bypassing FA.")
                desk.append_artifact("fundamental_report", {
                    "summary": "Skipped detailed fundamental analysis due to High Volatility regime. Quantitative metrics prioritized.",
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
            if regime != "HIGH_VOLATILITY":
                _queue_agent("fundamental_analyst", fundamental_analyst, parent="junior_analyst")
                _queue_agent("quant_analyst", quant_analyst, parent="junior_analyst")
                
        elif sec in ("fundamental_report", "quant_report"):
            # Check if research tier is fully complete
            if regime == "HIGH_VOLATILITY":
                if desk.has_artifact("desk_note") and desk.has_artifact("quant_report"):
                    _queue_debate_phase()
            else:
                if desk.has_artifact("desk_note") and desk.has_artifact("fundamental_report") and desk.has_artifact("quant_report"):
                    _queue_debate_phase()
                    
        elif sec in ("bull_argument", "bear_rebuttal"):
            if desk.has_artifact("bull_argument") and desk.has_artifact("bear_rebuttal"):
                _queue_agent("debate_judge", debate_judge, parent="bull_argument")
                
        elif sec in ("debate_judge", "tournament_result"):
            _queue_agent("board_of_directors", None, parent="debate_judge")
            
        elif sec == "final_decision":
            if _settings.DECISION_AGENT_ENABLED:
                _queue_agent("decision_synthesizer", decision_agent, parent="board_of_directors")

    def _queue_debate_phase():
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
        try:
            task_section = await whiteboard.get_section(ticker=ticker, cycle_id=cycle_id, section="task_queue")
            if task_section and isinstance(task_section.get("content"), dict):
                tasks_list = task_section["content"].get("tasks", [])
                return any(t.get("status") == "pending" for t in tasks_list)
        except Exception as e:
            logger.warning("[V3] Error checking pending peer requests: %s", e)
        return False

    async def _process_peer_requests():
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
        try:
            from app.cognition.debate.tournament import run_tournament_debate
            from app.cognition.contracts.evidence import EvidencePacket
            from app.cognition.contracts.retrieval import StructuredFact

            facts = []
            for artifact_name in ("desk_note", "fundamental_report", "quant_report"):
                artifact = getattr(desk, artifact_name, None)
                if artifact and isinstance(artifact, dict):
                    summary = artifact.get("summary", "")
                    if summary:
                        facts.append(
                            StructuredFact(
                                fact_type=artifact_name,
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
                "jury_verdict": tournament_result.get("jury_verdict", {}),
                "vetoed": tournament_result.get("jury_verdict", {}).get("vetoed", False),
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

            emit(
                "analyzing", f"v3_tournament_done_{ticker}",
                f"🏆 {ticker}: Tournament complete → {tournament_result.get('action', 'HOLD')} "
                f"@ {tournament_result.get('confidence', 0)}% "
                f"(winner: {tournament_result.get('winning_side', 'split')})",
                status="ok",
            )
        except Exception as tournament_err:
            logger.error("[V3] %s: Tournament debate failed: %s", ticker, tournament_err, exc_info=True)
            logger.info("[V3] Falling back to classic debate agents (Bull/Bear).")
            _queue_agent("bull_argument", bull_agent, parent="tournament_debate")
            _queue_agent("bear_rebuttal", bear_agent, parent="tournament_debate")

    async def _persist_trade_verdict():
        if desk.has_artifact("trade_decision"):
            try:
                from app.services.trade_result_saver import save_trade_result
                trade_decision = desk.trade_decision or {}
                if not trade_decision.get("regime"):
                    trade_decision["regime"] = regime
                if not trade_decision.get("persona_used"):
                    board_decision = desk.final_decision or {}
                    trade_decision["persona_used"] = board_decision.get(
                        "persona_used", _persona_label(regime)
                    )
                save_trade_result(ticker, cycle_id, trade_decision)

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
    whiteboard.subscribe(whiteboard_subscriber)

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
                breaker.record_outcome("bull_argument", outcome)
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
                breaker.record_outcome("bear_rebuttal", outcome)
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
                    desk=desk, breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit
                )
                breaker.record_outcome("debate_judge", outcome)
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
                    desk=desk, regime=regime, breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit
                )
                breaker.record_outcome("board_of_directors", outcome)
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
            logger.warning("[V3] DynamicOrchestrator hit MAX_LOOP_ITERATIONS safeguard for %s.", ticker)

    finally:
        whiteboard.unsubscribe(whiteboard_subscriber)

    try:
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
    except Exception as e:
        logger.warning("[V3] %s: Memory persistence failed (non-fatal): %s", ticker, e)

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

    return result

def _apply_policy_gates(desk: SharedDesk) -> str:
    """Apply explicit orchestration policy gates to the final decision."""
    decision = desk.trade_decision or desk.final_decision or {}
    action = decision.get("action", "HOLD").upper()
    confidence = decision.get("confidence", 0)

    if action == "HOLD":
        return "HOLD_NO_SIGNAL"

    if confidence < 60:
        return "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"
        
    if not desk.has_artifact("regime_classification"):
        return "HOLD_POLICY_BLOCKED_MISSING_REGIME"

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


async def _run_debate_judge(
    desk: SharedDesk,
    breaker: CircuitBreaker,
    cycle_id: str,
    bot_id: str,
    emit: Any,
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
    bod_module.TOOL_WHITELIST = [
        "whiteboard_read", "whiteboard_write", "whiteboard_annotate", "whiteboard_summarize",
        "get_portfolio_state",  # Phase 2: contextual portfolio awareness
    ]
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
    dynamic_trigger = decision.get("dynamic_trigger")

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
            transcript_parts.append(f"  • {s.get('persona', '?')}: {s.get('claim', '')} (Backtest PnL: {s.get('backtest_pnl', 0):.2f}%)")
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
            "dynamic_trigger": dynamic_trigger
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
    if not desk.bull_argument and not desk.bear_rebuttal:
        return None

    def _safe_int(val, default=0):
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

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
