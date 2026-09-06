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
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from app.v3.shared_desk import (
    SharedDesk, DeskPhase, PhaseOutcome, DecisionProvenance,
)
from app.v3.guardrails import CircuitBreaker, research_degraded
from app.services.adaptive_concurrency import concurrency_controller
from app.v3.telemetry import persist_telemetry
from app.v3.agent_runner import run_v3_agent
from app.v3.desk_persistence import save_desk
from app.db import mongo_query
from app.db import mongo_store
from datetime import timedelta

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
    #: The cycle's other candidate names (app/v3/cycle_candidates.py). Defaults
    #: to None so every existing caller — tests, the scheduler, the watch desk
    #: — keeps working and simply gets no cross-ticker block.
    cycle_candidates: list[dict] | None = None,

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
        # `cycle-v3-` prefix, not a bare `v3-`: every report and live reader
        # identifies a production desk by that prefix
        # (`hold_wall_report._is_production_cycle`, `cycle_scope`), so an id
        # minted here as "v3-<uuid>" produced a real desk that the measurement
        # window, the hold-wall report and the contamination probes all skipped.
        # Never observed in the store (callers always pass a cycle_id), so this
        # is closing a latent hole, not repairing data. Keeps a random tail so
        # two tickers starting in the same second cannot collide.
        cycle_id = f"cycle-v3-{int(time.time())}-{uuid.uuid4().hex[:6]}"

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
        from app.tools.tool_context import tool_context
        from app.v3.data_report import build_ticker_data_report
        # Pre-collect runs before any agent, so nothing else would scope it —
        # and it is the noisiest stage in the cycle (six collectors, every
        # vendor refusal logged as a warning) as well as the slowest measured
        # one. Its warnings are worth attributing to a named stage.
        with tool_context(cycle_id=cycle_id, ticker=ticker, phase="precollect"):
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

    # ───────────────────────────────────────────────────────────────────
    # Concurrent Context Block Assembly (13 independent IO/Compute tasks)
    # ───────────────────────────────────────────────────────────────────
    async def _build_macro_task():
        try:
            from app.collectors.market_regime_collector import get_latest_market_snapshot
            macro_snapshot = await asyncio.to_thread(get_latest_market_snapshot)
            macro_briefing = _format_macro_briefing(macro_snapshot)
            if macro_briefing:
                desk.cycle_metadata["macro_briefing"] = macro_briefing
        except Exception as e:
            logger.warning("[V3] %s: macro snapshot unavailable (non-fatal): %s", ticker, e)

    async def _build_quant_math_task():
        try:
            from app.quant.context_block import build_quant_math_block
            quant_math = await asyncio.wait_for(
                asyncio.to_thread(build_quant_math_block, ticker, bot_id, cycle_id),
                timeout=60,
            )
            if quant_math:
                desk.cycle_metadata["quant_math_context"] = quant_math
                logger.info("[V3] %s: precomputed quant math injected (%d chars)",
                            ticker, len(quant_math))
        except asyncio.TimeoutError:
            logger.warning(
                "[V3] %s: quant math precompute TIMED OUT after 60s — GARCH, HRP and "
                "the sizing bracket are all MISSING from this desk", ticker,
            )
        except Exception as e:
            logger.warning("[V3] %s: quant math precompute failed (non-fatal): %s (%s)",
                           ticker, e, type(e).__name__)

    async def _build_technical_task():
        # TWO INDEPENDENT try blocks, deliberately.
        #
        # 22c95d8 folded the staleness probe into the baseline block's handler
        # while making these builders concurrent. That coupled them: a failed
        # or 15s-timed-out `build_technical_baseline_block` skipped detection
        # entirely, and `_apply_policy_gates` reads an ABSENT age as fresh — so
        # HOLD_POLICY_BLOCKED_STALE_PRICE_DATA stopped firing in exactly the
        # case it exists for (degraded price data), with no log line saying so.
        # The pre-22c95d8 code ran the probe in its own try for this reason.
        #
        # Fail-open is still the policy here, unchanged since 2026-07-31: an
        # unreadable baseline must not block a cycle, and the executor-side
        # position/price checks are the backstop. What changes is that a failed
        # probe is now RECORDED instead of being indistinguishable from fresh.
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
                logger.warning(
                    "[V3] %s: technical baseline came back EMPTY — the desk has no "
                    "verified indicator anchor for this ticker", ticker,
                )
        except Exception as e:
            logger.warning("[V3] %s: technical baseline failed (non-fatal): %s", ticker, e)

        try:
            from app.quant.technical_baseline import compute_technical_baseline

            # Staleness detection
            _b = await asyncio.wait_for(
                asyncio.to_thread(compute_technical_baseline, ticker), timeout=10,
            )
            _trd = _stale_age((_b or {}).get("age_trading_days"))
            if _trd is not None and _trd > 3:
                from app.v3.telemetry import record_guardrail_firing

                desk.cycle_metadata["stale_price_age_trading_days"] = _trd
                desk.cycle_metadata["stale_price_as_of"] = str((_b or {}).get("as_of"))
                record_guardrail_firing(
                    "STALE_PRICE_DATA",
                    ticker=ticker,
                    cycle_id=cycle_id or "",
                    detail={
                        "age_trading_days": _trd,
                        "as_of": str((_b or {}).get("as_of")),
                        "shadow": False,
                        "enforced_by": "HOLD_POLICY_BLOCKED_STALE_PRICE_DATA",
                    },
                )
                logger.warning(
                    "[V3] %s: STALE price data — baseline is %d trading day(s) old; "
                    "any BUY/SELL from this desk will be policy-blocked", ticker, _trd,
                )
        except Exception as e:
            # The age is UNKNOWN, which is not the same fact as "fresh". Stamp
            # it so the desk carries the distinction the gate cannot express,
            # and count it in shadow so the operator can see how often the
            # stale-price gate is running blind before anyone argues about
            # promoting this to a hard block.
            desk.cycle_metadata["stale_price_detection_failed"] = (
                f"{type(e).__name__}: {e}"[:200]
            )
            logger.warning(
                "[V3] %s: staleness detection skipped: %s — price age is UNKNOWN for "
                "this desk, so HOLD_POLICY_BLOCKED_STALE_PRICE_DATA cannot fire",
                ticker, e,
            )
            try:
                from app.v3.telemetry import record_guardrail_firing

                record_guardrail_firing(
                    "STALE_PRICE_DETECTION_FAILED",
                    ticker=ticker,
                    cycle_id=cycle_id or "",
                    detail={
                        "error": f"{type(e).__name__}: {e}"[:200],
                        "shadow": True,
                        "enforced_by": None,
                    },
                )
            except Exception:  # noqa: BLE001 — telemetry must never fail a desk
                pass

    async def _build_valuation_task():
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

    async def _build_fundamental_task():
        try:
            from app.quant.fundamental_block import build_fundamental_block
            fund_block = await asyncio.wait_for(
                asyncio.to_thread(build_fundamental_block, ticker),
                timeout=10,
            )
            if fund_block:
                desk.cycle_metadata["fundamental_context"] = fund_block
                logger.info("[V3] %s: precomputed fundamental snapshot injected (%d chars)",
                            ticker, len(fund_block))
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

    async def _build_decision_score_task():
        try:
            from app.quant.decision_score import (
                build_decision_score_block, compute_decision_score,
            )

            def _score_and_render():
                s = compute_decision_score(ticker)
                return s, build_decision_score_block(ticker, s)

            score, score_block = await asyncio.wait_for(
                asyncio.to_thread(_score_and_render), timeout=15,
            )
            desk.cycle_metadata["decision_score"] = score
            try:
                from app.quant.decision_score_store import record_decision_score
                await asyncio.to_thread(
                    record_decision_score, cycle_id, ticker, score)
            except Exception as e:
                logger.debug("[V3] %s: baseline not recorded (non-fatal): %s",
                             ticker, e)
            if score_block:
                desk.cycle_metadata["decision_score_context"] = score_block
                logger.info(
                    "[V3] %s: deterministic baseline injected — %s %s "
                    "(conf %s, coverage %s%%, R:R %s)",
                    ticker, score.get("band"), score.get("score"),
                    score.get("confidence"), score.get("coverage_pct"),
                    (score.get("risk_reward") or {}).get("ratio"),
                )
        except asyncio.TimeoutError:
            logger.warning(
                "[V3] %s: deterministic baseline TIMED OUT after 15s — the desk has "
                "no computed composite, no R:R and no structural gates", ticker,
            )
        except Exception as e:
            logger.warning("[V3] %s: deterministic baseline failed (non-fatal): "
                           "%s (%s)", ticker, e, type(e).__name__)

    async def _build_candidate_pool_task():
        try:
            from app.v3.cycle_candidates import build_candidate_block, shown_tickers
            from app.v3.substitute import POOL_KEY

            candidate_block = build_candidate_block(cycle_candidates, self_ticker=ticker)
            if candidate_block:
                desk.cycle_metadata["cycle_candidates_context"] = candidate_block
                pool = shown_tickers(cycle_candidates, self_ticker=ticker)
                desk.cycle_metadata[POOL_KEY] = pool
                logger.info(
                    "[V3] %s: cross-ticker candidates injected — %d alternatives",
                    ticker, len(pool),
                )
        except Exception as e:
            logger.warning("[V3] %s: candidate block failed (non-fatal): %s", ticker, e)

        try:
            from app.v3.substitute import POOL_KEY
            from app.v3.wake_pool import build_wake_pool, build_wake_pool_block

            if desk.cycle_metadata.get("held") is True and not desk.cycle_metadata.get(POOL_KEY):
                record = build_wake_pool(ticker, exclude_cycle_id=cycle_id)
                desk.cycle_metadata["substitute_ask_skipped"] = record.get("reason")
                block = build_wake_pool_block(record, self_ticker=ticker)
                if block:
                    desk.cycle_metadata["cycle_candidates_context"] = block
                    desk.cycle_metadata[POOL_KEY] = list(record["tickers"])
                    desk.cycle_metadata["wake_pool"] = {
                        "source_cycle_id": record.get("cycle_id"),
                        "age_hours": record.get("age_hours"),
                        "n": len(record["tickers"]),
                    }
                    logger.info(
                        "[V3] %s: HELD name with no pool — borrowed %d names from "
                        "%s (%sh old)", ticker, len(record["tickers"]),
                        record.get("cycle_id"), record.get("age_hours"),
                    )
                else:
                    logger.info(
                        "[V3] %s: HELD name with no pool and none borrowable (%s) "
                        "— the bear cannot be asked for a substitute",
                        ticker, record.get("reason"),
                    )
        except Exception as e:
            logger.warning("[V3] %s: wake pool failed (non-fatal): %s: %s",
                           ticker, type(e).__name__, e)

    async def _build_opinion_task():
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

    async def _build_alt_data_task():
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

    async def _build_book_brief_task():
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

    async def _build_memory_task():
        try:
            from app.services.parameter_store import get_param
            _memory_on = bool(get_param("MEMORY_CONTEXT_ENABLED"))
        except Exception:
            _memory_on = False
        if not _memory_on:
            desk.cycle_metadata["memory_context_state"] = "off"
            return
        try:
            from app.services.memory.retriever import MemoryRetriever
            retrieval_results = await asyncio.to_thread(MemoryRetriever.retrieve, ticker=ticker)
            brief_text = ""
            if retrieval_results:
                memory_brief = MemoryRetriever.build_memory_brief(retrieval_results)
                brief_text = memory_brief.get("brief_text", "")

            addenda = ""
            try:
                from app.services.retrieval_context import build_memory_addenda
                addenda = await asyncio.to_thread(build_memory_addenda, ticker)
            except Exception as addenda_err:
                logger.debug("[V3] %s: memory addenda failed (non-fatal): %s",
                             ticker, addenda_err)

            combined = "\n\n".join(b for b in (brief_text, addenda) if b)
            if combined:
                desk.cycle_metadata["memory_context"] = combined
                desk.cycle_metadata["memory_context_state"] = f"on:{len(combined)}"
                logger.info(
                    "[V3] %s: Injected memory context (%d canonical entries, %d chars total)",
                    ticker, len(retrieval_results or []), len(combined),
                )
            else:
                desk.cycle_metadata["memory_context_state"] = "on:empty"
        except Exception as e:
            desk.cycle_metadata["memory_context_state"] = f"crashed:{type(e).__name__}"
            logger.warning("[V3] %s: Memory retrieval failed (non-fatal): %s", ticker, e)

    previous_desk = None

    async def _build_previous_desk_task():
        nonlocal previous_desk
        try:
            from app.v3.desk_persistence import load_latest_desk_for_ticker
            previous_desk = await asyncio.to_thread(load_latest_desk_for_ticker, ticker)
            if previous_desk:
                prev_context = previous_desk.get_handoff_brief()
                if prev_context and prev_context != "No artifacts on desk yet.":
                    desk.cycle_metadata["previous_desk_context"] = prev_context
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

    # Autoresearch directives (instant in-memory filter)
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

    # Execute all 11 context builders in parallel
    await asyncio.gather(
        _build_macro_task(),
        _build_quant_math_task(),
        _build_technical_task(),
        _build_valuation_task(),
        _build_fundamental_task(),
        _build_decision_score_task(),
        _build_candidate_pool_task(),
        _build_opinion_task(),
        _build_alt_data_task(),
        _build_book_brief_task(),
        _build_memory_task(),
        _build_previous_desk_task(),
        return_exceptions=True,
    )

    # Deterministic Data Readiness Gate (shadow evaluation before Phase 0 triage)
    try:
        from app.v3.data_readiness import evaluate_ticker_readiness
        readiness = evaluate_ticker_readiness(
            ticker=ticker,
            data_report=data_report,
            technical_context=desk.cycle_metadata.get("technical_baseline_context"),
            valuation_context=desk.cycle_metadata.get("valuation_context"),
            price_age_trading_days=desk.cycle_metadata.get("stale_price_age_trading_days"),
            stale_detection_failed=bool(
                desk.cycle_metadata.get("stale_price_detection_failed")
            ),
        )
        desk.cycle_metadata["readiness"] = {
            "is_ready": readiness.is_ready,
            "quality_score": readiness.quality_score,
            "missing_reasons": readiness.missing_reasons,
            "disposition": readiness.disposition,
        }
    except Exception as read_err:
        logger.warning("[V3] %s: data readiness check failed (non-fatal): %s", ticker, read_err)

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
    # Mirrored onto the desk because `_record_gate` stamps every policy-gate
    # firing with `cycle_metadata["triage_tier"]` — and nothing ever wrote that
    # key, so all 30 firings over 21 days recorded `triage_tier: null` and the
    # question its own comment poses ("how often does the delta path block?")
    # stayed exactly as unanswerable as before the field was added.
    # Re-stamped at every reassignment below; this is the TRIAGE_ENABLED=False
    # default.
    desk.cycle_metadata["triage_tier"] = triage_tier
    if settings.TRIAGE_ENABLED:
        try:
            from app.db import mongo_store
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            news_count = mongo_store.count_docs("news_articles", {"ticker": ticker, "published_at": {"$gte": since}})
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

        desk.cycle_metadata["triage_tier"] = triage_tier
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
            # DELIBERATELY NO `_attach_hold_reason` HERE. This HOLD is an
            # age/news heuristic that ran before any agent, and `classify_hold`
            # would return WATCH — "thesis constructive, not entering yet" — a
            # claim nobody made. It is the same error the 07-25 audit fixed by
            # stamping TRIAGE_SKIP: an unreasoned HOLD read as a board opinion.
            # Labelling it would also pollute the only test of whether the
            # split works (does AVOID concentrate in the low band?) with rows
            # carrying confidence 0 and no thesis at all.
            #
            # It was unlabelled before this commit too — but by accident,
            # because the single call site sat below an early return. Stated
            # here so the next coverage sweep does not "fix" it.
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

                # Write the trade row (2026-08-03). The delta tier publishes
                # only `final_decision`, so the old `has_artifact("trade_decision")`
                # gate meant this whole route persisted NOTHING to trade_results
                # — 40 of 40 delta analyses over 21 days, 5 of which executed
                # real orders. Its decisions were invisible to P&L, the
                # scorecard, strategy tracking and the LLM judge, and the
                # policy_action UPDATE below had no row to land on.
                #
                # Ordered BEFORE the gates and the result build so the level
                # sanitizer inside it runs first: the delta path used to build
                # `result` before the gates, so a decimal-error stop survived in
                # result["estimate"] and reached buy() as a live stop order.
                await _persist_trade_verdict(
                    desk, _delta_decision,
                    cycle_id=cycle_id, bot_id=bot_id, ticker=ticker,
                    # The regime engine has not run at this point — Phase 0 sits
                    # above it — so there is no cycle regime to inherit. The
                    # delta artifact already carries its own ("delta_relook"),
                    # which is why this fallback is never actually consumed.
                    regime=(desk.regime_classification or {}).get("regime", "delta_relook"),
                    source="v3_delta",
                )

                save_desk(desk)
                # Layer 6 lives at the end of the full-panel flow, which this
                # early return skips. Without this, `policy_action` is unset and
                # pipeline_service's enforcement branches (which key off it)
                # cannot match — so a delta BUY skipped the low-confidence,
                # missing-regime and strategy-health CUT gates entirely, and was
                # sized without the consensus/data-quality haircuts.
                policy_action = _apply_policy_gates(desk)
                elapsed_s = time.monotonic() - t_pipeline
                result = _build_v1_compatible_result(desk, elapsed_s=elapsed_s)
                result["triage_tier"] = "v3_delta"
                result["escalated"] = False
                result["policy_action"] = policy_action
                # The label has to be attached HERE too — this return is ~1,600
                # lines above the full-panel call site, and a delta HOLD is
                # still a HOLD that means one of two different things.
                _attach_hold_reason(result, desk=desk, ticker=ticker, emit=emit)
                _attach_exit_shadow(result, ticker=ticker)
                _attach_confidence_shadow(result, ticker=ticker)
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
            desk.cycle_metadata["triage_tier"] = triage_tier
            # continue below to the full blackboard panel

    # ═══════════════════════════════════════════════════════════════════
    # DYNAMIC BLACKBOARD / P2P COORDINATOR
    # ═══════════════════════════════════════════════════════════════════
    from app.v3.agents import regime_engine
    from app.v3.agents import junior_analyst, fundamental_analyst, quant_analyst
    from app.v3.agents import valuation_analyst
    from app.v3.agents import bull_agent, bear_agent, bull_defense, debate_judge
    from app.v3.agents import decision_agent
    from app.config.config_cognition import cognition_settings as _cog_settings
    from app.config import settings as _settings

    tasks_to_run = []
    
    # Track execution counts to prevent infinite cascades / loops
    MAX_RUNS_PER_AGENT = 3
    # The bull defense gets one retry before the debate is conceded — see the
    # bull_defense branch. Must stay strictly below MAX_RUNS_PER_AGENT so the
    # retry can actually be queued.
    DEFENSE_MAX_ATTEMPTS = 2
    run_counts = {
        "regime_engine": 0,
        "junior_analyst": 0,
        "fundamental_analyst": 0,
        "quant_analyst": 0,
        "valuation_analyst": 0,
        "bull_argument": 0,
        "bear_rebuttal": 0,
        # Missing keys are fatal here, not merely untracked: _queue_agent reads
        # through run_counts.get(), but the scheduler loop does
        # `run_counts[name] += 1` and would KeyError the whole desk.
        "bull_defense": 0,
        "debate_judge": 0,
        "board_of_directors": 0,
        "decision_synthesizer": 0,
    }

    # UNCLASSIFIED, not CONTRADICTORY (open item 2, 2026-08-05): an engine
    # that never classified must stay distinguishable from one that genuinely
    # read a contradictory tape. get_persona_prompt treats any unknown label
    # as Jane Street with a warning, so dispatch behavior is unchanged — but
    # the artifact trail and logs no longer manufacture a real-looking regime.
    regime = "UNCLASSIFIED"
    fa_skipped = False  # set when the Regime Engine recommends skipping FA
    # Dispatch-once latches for the decision layer. Any analyst re-run that
    # re-writes a research section would otherwise re-fire the whole
    # debate→board→synth chain (observed live: 1 ticker → tournament×2,
    # board×2, synth×2, ~2x compute). The mechanism that produced those
    # re-runs -- peer requests -- was removed on 2026-08-28, but an agent can
    # still run up to MAX_RUNS_PER_AGENT times, so the latches stay.
    # The debate consumes a SNAPSHOT of research; re-running analysts after it
    # has started cannot change a verdict already rendered, so we latch each
    # decision-layer stage to a single dispatch.
    debate_dispatched = False
    board_dispatched = False
    synth_dispatched = False
    research_tier_dispatched = False

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
        nonlocal regime, fa_skipped, debate_dispatched, board_dispatched, \
            synth_dispatched, research_tier_dispatched
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
        # UNCLASSIFIED, re-queue FA/QA, or re-trigger the debate chain.
        if event.get("type") != "whiteboard_update":
            return
        sec = event.get("section")
        auth = event.get("author")
        logger.info("[V3] Whiteboard event trigger: section '%s' updated by '%s'", sec, auth)

        # ── The transcript ────────────────────────────────────────────────
        # Every artifact completion passes through here, which makes this the
        # one place a chat line can be emitted without threading an emit call
        # through eleven agent call sites. The debate was otherwise invisible
        # live: `debate_pitch`/`clash`/`vote`/`verdict` are emitted only from
        # the tournament path and fired ZERO times in the 7 days to
        # 2026-08-08, so what each agent actually said reached the operator
        # only after the whole ticker finished and the desk row was written.
        try:
            from app.v3.agent_chat import chat_line_for, emit_agent_message

            _line = chat_line_for(sec, event.get("content"))
            if _line:
                emit_agent_message(
                    emit,
                    speaker=_line["speaker"],
                    ticker=ticker,
                    text=_line["text"],
                    role=_line["role"],
                    stance=_line["stance"],
                    confidence=_line["confidence"],
                    extra=_line["extra"],
                )
        except Exception as _chat_err:  # noqa: BLE001 — an observer never blocks
            logger.debug("[V3] chat line not emitted: %s", _chat_err)


        if sec == "regime_classification":
            content = event.get("content") or {}
            if "regime" not in content:
                # Same rationale as the UNCLASSIFIED initializer above: a
                # regime artifact without a label is a failed classification,
                # not a contradictory tape.
                logger.warning(
                    "[V3] regime_classification artifact carries no 'regime' "
                    "field — treating as UNCLASSIFIED, not CONTRADICTORY"
                )
            regime = content.get("regime", "UNCLASSIFIED")

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
            # Latch, same pattern as debate_dispatched. A desk_note RE-write is
            # a repeat of work already done, not a new research phase — but
            # this branch used to process it as one, re-queueing FA+QA+VA whose
            # runs were already complete (_queue_agent dedupes only against
            # PENDING tasks). Measured in cycle-v3-1785504601: 4 of 6 tickers
            # carried one peer request to the JA, and each became 4 duplicate
            # analyst runs — 17 of the cycle's 18 extra runs, the "~1.4
            # runs/ticker" waste the 07-30 handoff ranked #1. The re-written
            # note still lands on the whiteboard where the debate and Board
            # read it; nothing downstream needs the re-dispatch. The latch also
            # keeps a re-run's triage from firing twice — a second desk_note
            # recommending SKIP used to clear the task queue MID-debate.
            if research_tier_dispatched:
                logger.info(
                    "[V3] %s: desk_note re-write (peer-request answer) — "
                    "research tier already dispatched, not re-dispatching",
                    ticker,
                )
                return
            research_tier_dispatched = True

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
                    # `fa_skipped` MUST be cleared, or this override queues
                    # NOTHING (2026-07-29). The branch chain below is
                    # SKIP / (QUANT_ONLY and not fa_skipped) / (not fa_skipped)
                    # — so when the Regime Engine already set fa_skipped=True,
                    # an override to FULL matches no branch at all and the
                    # pipeline dead-ends here: no FA, no QA, no valuation, no
                    # debate, no board. The comment above says this "can only
                    # ever ADD work"; without this line it silently removed all
                    # of it.
                    #
                    # Observed on ORCL (cycle-v3-1785304312): the JA published
                    # desk_note, this override fired, and the ticker then ran
                    # 215s to "HOLD @ 0%, persona: unknown" with NO shared_desk
                    # row and NO trade_results row. 5 such tickers since 07-28
                    # (AMID, GM, WMT, JPM, ORCL) — 0% before that, 15% on 07-29.
                    #
                    # Clearing it is the conservative direction: FA was skipped
                    # on a recommendation made when research was degraded, and
                    # the whole point of the override is to distrust that
                    # recommendation. _queue_agent dedupes on pending tasks and
                    # MAX_RUNS_PER_AGENT caps re-runs, so re-queueing an
                    # already-queued QA is a no-op.
                    if fa_skipped:
                        logger.warning(
                            "[V3] %s: clearing fa_skipped — FA was skipped upstream "
                            "but research was degraded, so the FULL override must "
                            "re-queue it rather than queue nothing.", ticker,
                        )
                        fa_skipped = False

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
            # Three turns, not two (2026-08-05). The Bear reads the Bull's
            # thesis and adds risks the Bull never anticipated; without a reply
            # turn it won 72-94% of 288 debates, and a bear win in a long-only
            # book can only become HOLD. The judge now waits for the defense.
            #
            # The defense is NOT a barrier: the task-loop branch queues the
            # judge whether the defense succeeds or fails, so a dead defense
            # agent degrades the debate rather than stranding the desk without
            # a decision (the FDVV failure shape).
            if desk.has_artifact("bull_argument") and desk.has_artifact("bear_rebuttal"):
                _queue_agent("bull_defense", bull_defense, parent="bear_rebuttal")

        elif sec == "bull_defense":
            _queue_agent("debate_judge", debate_judge, parent="bull_defense")


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
                deep_block = await run_deep_retrieval_for_synthesizer(
                    ticker=ticker,
                    judge_confidence=_judge_confidence(desk.debate_judge or {}),
                    emit=emit,
                )
                if deep_block:
                    desk.cycle_metadata["deep_retrieval_context"] = deep_block
                _queue_agent("decision_synthesizer", decision_agent, parent="board_of_directors")

    def _queue_debate_phase():
        nonlocal debate_dispatched, board_dispatched
        # Latch: the debate runs once on a research snapshot. An analyst
        # re-run that re-writes fundamental_report/quant_report must NOT
        # re-queue the debate.
        if debate_dispatched:
            return
        debate_dispatched = True

        # Frame the debate before anyone argues (2026-08-05). Deterministic
        # over artifacts already on the desk — no model call, no added cycle
        # cost — so the propositions are auditable after the fact. Computed
        # HERE, once, at the latch: every debate participant must argue the
        # same questions, and a per-agent recompute could drift as later
        # artifacts land. Non-fatal: without it the agents fall back to the
        # generic "is this a buy" debate.
        try:
            from app.v3.debate_frame import build_debate_frame_block, derive_debate_frame

            _frame = derive_debate_frame(desk)
            desk.cycle_metadata["debate_frame"] = _frame
            desk.cycle_metadata["debate_frame_context"] = build_debate_frame_block(
                ticker, _frame
            )
            logger.info(
                "[V3] %s: debate framed as %s (%d candidates considered)",
                ticker, _frame.get("keys"), _frame.get("considered", 0),
            )
        except Exception as _frame_err:  # noqa: BLE001 — never block the debate
            logger.warning(
                "[V3] %s: debate framing failed (%s) — falling back to the "
                "unframed debate", ticker, _frame_err,
            )

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

        # The desk gets an adversarial pass: bull argues, bear rebuts, and the
        # judge chains the Board through the same whiteboard subscriber the
        # tournament used (`elif sec in ("debate_judge", "tournament_result")`).
        #
        # The 4-persona pitch/h2h/jury tournament that used to run here was
        # retired on measurement (~30% of pipeline spend, and its jury veto
        # blocked ZERO decisions ever) and is now deleted. It had in fact been
        # dormant far longer than the retirement flag suggested: TOURNAMENT_MODE
        # defaulted True and routed every debate to the tournament, leaving the
        # bull/bear branch unreachable, so 987 of 1340 desks carry
        # `bull_argument: null` and the last desk with real bull text is
        # 2026-07-12. Running bull/bear here is the RESTORATION of that path.
        #
        # No skip marker and no explicit Board dispatch: bull_argument +
        # bear_rebuttal chain debate_judge, which chains the Board. A second
        # dispatch would fight the latch.
        _queue_agent("bull_argument", bull_agent, parent="quant_analyst")
        _queue_agent("bear_rebuttal", bear_agent, parent="quant_analyst")

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

        while tasks_to_run and loop_counter < MAX_LOOP_ITERATIONS:
            loop_counter += 1

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
                # include_debate_context=True or the bear never sees the bull:
                # its prompt promises "the Bull Analyst's thesis" and mandates
                # bull_claim_addressed, but without this flag
                # get_compressed_context drops bull_argument and the bear
                # rebuts a reconstruction (measured 08-04: 9/9 big-cycle bears
                # confabulated the bull's claims, incl. a fabricated quote).
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk, agent_module=module, phase_name="bear_rebuttal",
                    breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    include_debate_context=True,
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

            elif name == "bull_defense":
                # The debate's third turn. include_debate_context=True for the
                # same reason the bear needs it — a defense that cannot read
                # the rebuttal answers a reconstruction of it.
                outcome = await _run_agent_with_circuit_breaker(
                    desk=desk, agent_module=module, phase_name="bull_defense",
                    breaker=breaker, cycle_id=cycle_id, bot_id=bot_id, emit=emit,
                    include_debate_context=True,
                    custom_instructions=query, parent_agent=parent
                )
                # DELIBERATELY NOT _check_abort. A failed defense must not kill
                # the desk: the debate is merely incomplete, and the judge is
                # instructed to handle a missing reply by not awarding the bear
                # points the bull never got to answer. Aborting here would
                # trade a 72-94% bear bias for a desk that reaches no decision
                # at all, which is strictly worse.
                if outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP) and desk.bull_defense:
                    await whiteboard.write_section(
                        ticker=ticker, cycle_id=cycle_id,
                        section="bull_defense",
                        content=desk.bull_defense,
                        author_agent="v3_bull_defense"
                    )
                elif run_counts.get("bull_defense", 1) < DEFENSE_MAX_ATTEMPTS:
                    # RETRY BEFORE FAILING OPEN (2026-08-11). Measured over 185
                    # post-fix debates: when the defense turn is missing the bear
                    # wins 79% of the time, against 50% when it runs — the
                    # fail-open path silently restores the pre-fix two-turn
                    # debate for 18% of desks. One retry costs one agent call;
                    # conceding the debate costs the decision.
                    logger.warning(
                        "[V3] %s: bull_defense produced no artifact (%s) — "
                        "retrying (attempt %d of %d) before conceding the debate",
                        ticker, outcome, run_counts.get("bull_defense", 1),
                        DEFENSE_MAX_ATTEMPTS,
                    )
                    emit("analyzing", f"v3_defense_retry_{ticker}",
                         f"🔁 {ticker}: bull defense retry "
                         f"{run_counts.get('bull_defense', 1)}/{DEFENSE_MAX_ATTEMPTS}",
                         status="warn")
                    _queue_agent("bull_defense", module, query=query, parent=parent)
                else:
                    # Fail-open: the whiteboard write is what chains the judge,
                    # so a failed defense would otherwise strand the desk with
                    # a debate and no verdict. Reached only after the retry above
                    # also failed — recorded so the rate stays measurable.
                    logger.warning(
                        "[V3] %s: bull_defense produced no artifact (%s) after "
                        "%d attempts — chaining the judge on an incomplete debate",
                        ticker, outcome, run_counts.get("bull_defense", 1),
                    )
                    desk.cycle_metadata["defense_failed_open"] = True
                    emit("analyzing", f"v3_defense_failed_open_{ticker}",
                         f"⚠️ {ticker}: debate judged without a bull defense",
                         status="warn")
                    _queue_agent("debate_judge", debate_judge, parent="bear_rebuttal")

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
                
            elif name == "board_of_directors":
                if desk.phase == DeskPhase.RESEARCH_DONE:
                    desk.advance_phase(DeskPhase.DEBATE_DONE)
                    save_desk(desk)
                    emit("analyzing", f"v3_debate_done_{ticker}", f"⚔️ {ticker}: Debate layer complete", status="ok")

                # Cross-desk dissent, detected BEFORE the desk decides (2026-08-03).
                # The same detector used to run only at the end and silently
                # rewrite `confidence` to 60 — which the 70 floor always blocked,
                # so a "cap, not a downgrade" was a guaranteed downgrade and the
                # flagged trades could never resolve to prove the gate right.
                #
                # Moved in front of the decision instead: the board is told which
                # desks disagree and must answer them in `dissent_resolution`.
                # Nothing here changes a number — the enforcement is the
                # HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT backstop, which fires
                # only when the dissent went UNANSWERED.
                #
                # Runs pre-decision on purpose: at this point the desk carries no
                # final_decision/trade_decision, so the detector sees exactly the
                # research + debate artifacts and cannot mistake the agent's own
                # verdict for a corroborating source.
                try:
                    from app.v3.contradiction_shadow import (
                        build_dissent_block, compute_contradiction_shadow,
                    )
                    _pre = compute_contradiction_shadow(desk)
                    _dissent_block = build_dissent_block(_pre)
                    if _dissent_block:
                        desk.cycle_metadata["dissent_context"] = _dissent_block
                        desk.cycle_metadata["dissent_detected"] = {
                            "sentiment_by_source": _pre.get("sentiment_by_source"),
                            "contradiction_count": _pre.get("contradiction_count"),
                        }
                        logger.warning(
                            "[V3] %s: cross-desk dissent detected pre-decision (%s) "
                            "— board must answer it in dissent_resolution",
                            ticker, _pre.get("sentiment_by_source"),
                        )
                        emit(
                            "analyzing", f"v3_dissent_{ticker}",
                            f"🔀 {ticker}: desks disagree on direction "
                            f"({_pre.get('sentiment_by_source')}) — the board must "
                            f"resolve it in writing to trade",
                            status="warning",
                        )
                except Exception as _de:  # noqa: BLE001 — never block on detection
                    logger.warning(
                        "[V3] %s: pre-decision dissent detection failed "
                        "(non-fatal): %s", ticker, _de,
                    )

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
                        # A timeout and a board that failed for any other
                        # reason are different diagnoses and the enum has
                        # always had a member for each — but nothing ever
                        # wrote TIMEOUT_ABORT, so every timed-out board was
                        # recorded as a generic degrade and the distinction
                        # the enum advertised did not exist in the data.
                        # `outcome` already carries it; both values are in
                        # _DEGRADED_PROVENANCE, so scoring is unchanged and
                        # only the diagnosis gets sharper.
                        "decision_provenance": (
                            DecisionProvenance.TIMEOUT_ABORT.value
                            if outcome == PhaseOutcome.TIMED_OUT
                            else DecisionProvenance.BOARD_DEGRADED_FALLBACK.value
                        ),
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
                await _persist_trade_verdict(
                    desk, desk.trade_decision,
                    cycle_id=cycle_id, bot_id=bot_id, ticker=ticker,
                    regime=regime, source="v3_full",
                )

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
        # PERSIST ANYWAY (2026-07-29). `save_desk` used to live only inside the
        # try, so an illegal phase transition lost the entire desk: no
        # shared_desk row, and therefore no trade_results row either, because
        # _persist_trade_verdict is gated on has_artifact("trade_decision").
        #
        # The ticker still consumed the full pipeline budget. ORCL
        # (cycle-v3-1785304312) burned 215s and vanished; 5 tickers since 07-28
        # (AMID, GM, WMT, JPM, ORCL), 0% before that and 15% on 07-29 — an
        # invisible and RISING failure rate, which is the worst combination.
        #
        # A desk that died mid-pipeline is exactly the desk worth keeping: it
        # is the only record of where the pipeline stopped. Stamped so it can
        # never be mistaken for a reasoned HOLD — the degraded sentinel already
        # means "the pipeline tried to decide and failed", which is precisely
        # what happened here.
        try:
            desk.cycle_metadata["pipeline_incomplete"] = {
                "terminal_phase": str(desk.phase),
                "error": str(e),
                "had_final_decision": desk.has_artifact("final_decision"),
            }
            if not desk.has_artifact("final_decision"):
                desk.append_artifact("final_decision", {
                    "action": None,
                    "confidence": 0,
                    "reasoning": (
                        f"Pipeline ended at {desk.phase} without producing a "
                        f"decision: {e}"
                    ),
                    "decision_provenance": DecisionProvenance.BOARD_DEGRADED_FALLBACK.value,
                })
            save_desk(desk)
            logger.warning(
                "[V3] %s: persisted the incomplete desk at phase=%s so the "
                "failure is countable rather than invisible.", ticker, desk.phase,
            )
        except Exception as persist_err:  # noqa: BLE001 — never mask the original
            logger.error(
                "[V3] %s: could not persist the incomplete desk: %s",
                ticker, persist_err,
            )
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
    # Sanitize once more before the result is built. `_persist_trade_verdict`
    # already ran this, but ONLY when the synthesizer produced a decision — a
    # desk that stops at the Board (DECISION_AGENT_ENABLED off, or a degraded
    # synthesizer) would otherwise reach the executor with an unchecked level.
    # Idempotent: an already-dropped level is None and is skipped.
    _drop_implausible_levels(desk)

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
    # DOSSIER SYNC — the write side of the persistent research loop.
    # Runs AFTER the policy gates so the recorded action is the enforced one:
    # a BUY that a gate blocked must not enter the dossier as a BUY.
    # Non-fatal by construction and reads no decision — see dossier_sync.
    # ═══════════════════════════════════════════════════════════════════
    try:
        from app.v3.dossier_sync import sync_desk_to_dossier
        _dossier = sync_desk_to_dossier(
            desk,
            cycle_id=cycle_id,
            action=_decided.get("action"),
            confidence=_decided.get("confidence") or 0,
            policy_action=policy_action,
        )
        if _dossier.get("questions_found") or _dossier.get("questions_dropped"):
            emit(
                "analyzing", f"v3_dossier_{ticker}",
                f"📒 {ticker}: {_dossier['questions_found']} open question(s) — "
                f"{_dossier['questions_new']} new, {_dossier['questions_reasked']} re-asked, "
                f"{_dossier['queued']} queued for deep dive",
                status="ok",
                data=_dossier,
            )
    except Exception as e:
        logger.warning("[V3] %s: dossier sync failed (non-fatal): %s", ticker, e)

    # ═══════════════════════════════════════════════════════════════════
    # BUILD RESULT — V1-compatible shape for downstream phases
    # ═══════════════════════════════════════════════════════════════════
    elapsed_s = time.monotonic() - t_pipeline
    result = _build_v1_compatible_result(desk, elapsed_s=elapsed_s)

    _attach_hold_reason(result, desk=desk, ticker=ticker, emit=emit)
    _attach_exit_shadow(result, ticker=ticker)
    _attach_confidence_shadow(result, ticker=ticker)

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

    # Postconditions (2026-07-29). Placed HERE, after every write this pipeline
    # is going to make, because the defects it catches are absences — a desk
    # that was never persisted, a trade row that was never written — and an
    # absence is only knowable once the writing is finished.
    #
    # Records, never raises: an observer that can abort a cycle is a new failure
    # mode, and these exist to watch failure modes that already ship.
    try:
        from app.v3.invariants import check_ticker_complete

        check_ticker_complete(
            ticker=ticker, cycle_id=cycle_id, desk=desk, result=result,
        )
    except Exception as inv_err:  # noqa: BLE001 — never let an observer break a cycle
        logger.debug("[V3] %s: invariant check failed (non-fatal): %s", ticker, inv_err)

    # Inject the actual policy action so upstream callers (like cycle_main) can respect it
    result["policy_action"] = policy_action

    # Record the tier the Triage Gate actually evaluated — _build_v1_compatible_result
    # hardcodes "v3_full", which made analysis_results.triage_tier wrong for
    # every deep/standard ticker (triage analytics grouped on a constant).
    result["triage_tier"] = triage_tier

    return result


def _attach_hold_reason(result: dict, *, desk: SharedDesk, ticker: str, emit) -> None:
    """Split the HOLD that means two different things (WATCH vs AVOID).

    Reads only artifacts already on the desk, adds no cycle cost, and
    deliberately does NOT change `action`, `confidence` or any policy gate: it
    is a label emitted alongside the decision. See `app/v3/hold_reason.py`.

    **CALLED FROM BOTH DECISION EXITS.** `classify_hold` shipped 2026-08-08
    already reading the delta tier's `trade_decision` shape — but the only call
    site was in the full-panel flow, ~1,600 lines below the delta tier's
    `return result`. So the route the callee was specifically written to handle
    was the one route that never reached it: measured 2026-08-08, **1 of the 3
    HOLDs since the deploy carried a label**, and 52 `v3_delta_done_*` decisions
    lifetime could never have carried one.

    That is the same seam and the same shape as the 2026-07-25 audit, which
    found this early return skipping the no-shorting guard and every policy
    gate. A guarded callee does not protect its call site; the fix is a helper
    that both exits call, not a second copy of the block.
    """
    try:
        from app.v3.hold_reason import classify_hold

        hold = classify_hold(desk, result.get("action"))
        if not hold:
            return
        result["hold_reason"] = hold["hold_reason"]
        result["hold_reason_signals"] = hold["signals"]
        result["hold_reason_basis"] = hold.get("basis")
        # The position state the label was computed FROM, stored beside it.
        # Without this a reader has to join back to `shared_desk` to find out
        # which branch produced a label — and the whole point of the branch is
        # that the two vocabularies are not comparable.
        result["hold_reason_held"] = hold.get("held")
        # The substitute travels WITH the label. An AVOID whose named
        # alternative is only reachable by re-reading the bear's artifact is an
        # AVOID nothing downstream can act on, which is the defect this whole
        # change exists to fix.
        result["hold_substitute"] = hold.get("substitute_ticker")
        sub_note = (
            f" -> {hold['substitute_ticker']}" if hold.get("substitute_ticker")
            else ""
        )
        emit(
            "analyzing", f"v3_hold_reason_{ticker}",
            f"🔍 {ticker}: HOLD classified as {hold['hold_reason']}{sub_note}"
            f" [{hold.get('basis')}]"
            + (f" ({', '.join(hold['signals'])})" if hold["signals"] else ""),
            status="ok",
            data=hold,
        )
    except Exception as e:  # noqa: BLE001
        # Non-fatal by construction: a label must never cost a decision.
        logger.warning("[V3] %s: hold classification failed (non-fatal): %s", ticker, e)


#: What a hysteresis design would use for the EXIT side. Records only; nothing
#: reads this to gate anything. See `_attach_exit_shadow`.
_EXIT_FLOOR_SHADOW = 55


def _attach_exit_shadow(result: dict, *, ticker: str) -> None:
    """Record how close a HELD name came to an exit. GATES NOTHING.

    `_apply_policy_gates` applies ONE floor to every action — `if confidence <
    floor: HOLD_POLICY_BLOCKED_LOW_CONFIDENCE`, before the `if action == "BUY"`
    branch below it. So it takes the same conviction to LEAVE a position as to
    OPEN one, on a book where doing nothing is the default. Proper hysteresis
    (a Schmitt trigger) requires the exit threshold to sit BELOW the entry
    threshold; this is the opposite, and it is a ratchet.

    WHAT THIS DOES **NOT** MEASURE, and the distinction matters: it does not
    count SELLs the floor blocked, because there were **none** — zero SELL
    actions across 149 desks in five days. A shadow of "which blocked SELLs
    would a lower floor have released?" would answer a constant 0 and look like
    evidence that the floor is harmless. The binding constraint is upstream:
    the board never proposes an exit at all.

    So it counts what IS non-constant — held names where the desk's own label
    says an exit signal exists (`EXIT_SIGNALLED`) and the stated confidence
    would have cleared an exit-side floor. That is the population a hysteresis
    design would convert, and it is countable today.

    Deliberately not a gate, for the same reason `_attach_confidence_shadow` is
    not: the confidence SCALE is itself under review, and moving a threshold on
    top of an unvalidated scale makes both unattributable.
    """
    try:
        if str(result.get("action") or "").strip().upper() != "HOLD":
            return
        if result.get("hold_reason_held") is not True:
            return

        from app.services.parameter_store import get_param

        try:
            floor = float(get_param("ANALYSIS_CONFIDENCE_THRESHOLD"))
        except Exception:  # noqa: BLE001
            floor = 70.0

        confidence = result.get("confidence")
        confidence = float(confidence) if isinstance(
            confidence, (int, float)) and not isinstance(confidence, bool) else None
        signalled = result.get("hold_reason") == "EXIT_SIGNALLED"

        result["exit_floor_shadow"] = {
            "entry_floor": floor,
            "exit_floor_if_asymmetric": _EXIT_FLOOR_SHADOW,
            "confidence": confidence,
            "exit_signalled": signalled,
            # The whole point, in one boolean: the desk said an exit signal
            # exists and was sure enough that an exit-side floor would have let
            # it act — and it emitted HOLD anyway.
            "would_have_cleared_exit_floor": bool(
                signalled and confidence is not None
                and confidence >= _EXIT_FLOOR_SHADOW
            ),
            "clears_entry_floor": bool(
                confidence is not None and confidence >= floor),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[V3] %s: exit shadow failed (non-fatal): %s: %s",
                       ticker, type(e).__name__, e)


def _attach_confidence_shadow(result: dict, *, ticker: str) -> None:
    """Record what a recalibrated confidence scale WOULD have said. Gates nothing.

    Attached at both decision exits for the same reason `_attach_hold_reason`
    is: the delta tier returns ~1,600 lines above the full panel, and a helper
    wired into one exit measures one route.

    Deliberately NOT a gate. `08-confidence-rebuild.md` requires stage 1 to
    ship behind a parameter and alone in its window; this change already
    carries three debate/substitute fixes, so the scale stays raw and only the
    counterfactual is stored. Reading these rows is what decides the cutover.
    """
    try:
        from app.quant.confidence_calibration import shadow_record
        from app.services.parameter_store import get_param

        floor = get_param("ANALYSIS_CONFIDENCE_THRESHOLD")
        rec = shadow_record(result.get("confidence"), floor)
        if rec:
            rec["action"] = result.get("action")
            result["confidence_shadow"] = rec
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[V3] %s: confidence shadow failed (non-fatal): %s", ticker, e)


def _persist_policy_action(cycle_id: str, ticker: str, policy_action: str) -> None:
    """Record the enforced policy label on the trade row (PG + Mongo mirror).

    A 0-rowcount UPDATE is REPORTED, not swallowed (2026-07-29 harness audit).
    This is a blind UPDATE: `save_trade_result` is the only INSERT and it runs
    from `_persist_trade_verdict` alone.

    Measured over 2026-07-24..29, the window where the column is fully deployed:

        tier=v3_deep    analysed=136  trade_row=131  policy=131
        tier=v3_delta   analysed= 15  trade_row=  0  policy=  0
        tier=v3_glance  analysed=  5  trade_row=  0  policy=  0

    ~13% of analysed tickers computed an enforced policy label that was written
    nowhere, so every funnel query silently under-counted and the delta tier was
    unmeasurable. The enforcement itself was never affected: pipeline_service
    reads `policy_action` off the in-memory result dict, not the DB.

    FIXED for the delta tier 2026-08-03. This docstring used to justify the hole
    by calling delta "a path that never produced a trade decision" — it does
    produce one, an agent's reasoned BUY/SELL that gets executed, and a later
    count found 40 of 40 delta analyses with no trade row and 5 of them holding
    real filled orders. The delta branch now calls `_persist_trade_verdict`
    explicitly, so a row exists by the time this UPDATE runs.

    GLANCE is still expected to match 0 rows, and that remains correct: it
    writes a hardcoded HOLD@0 before any agent runs and can never trade.

    Logged rather than repaired here on purpose. Inserting a synthetic row from
    this function would invent a trade record for a path that never produced a
    trade decision, and a Triage-Gate skip is a legitimate non-decision, not a
    degrade (see the _DEGRADED_PROVENANCE note below). The correct fix is at the
    call site, and it needs its own verification; this makes the hole VISIBLE so
    it cannot silently regrow.
    """
    try:
        from app.db import mongo_store
        mongo_store.update_docs(
            "trade_results",
            {"cycle_id": cycle_id, "ticker": ticker},
            {"$set": {"policy_action": policy_action}},
        )
    except Exception as me:
        logger.warning("[V3] mongo policy_action update failed (non-fatal): %s", me)


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


def _stale_age(value: Any) -> float | None:
    """Numeric trading-day age, or None when unreadable.

    The detection block and the gate both used `isinstance(..., int)`, so a
    float age (e.g. 10.0 from a vendor change) was never stashed AND never
    gated — the guard disarmed silently at both ends. bool is excluded because
    it passes an isinstance-int check while meaning something else entirely.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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


async def _persist_trade_verdict(
    desk: SharedDesk,
    decision: dict | None,
    *,
    cycle_id: str,
    bot_id: str,
    ticker: str,
    regime: str,
    source: str = "v3_full",
) -> None:
    """Write the trade row + its downstream records for a decided ticker.

    `decision` defaults to the synthesizer's `trade_decision`. The DELTA
    tier passes its own artifact explicitly (2026-08-03): it publishes only
    `final_decision`, so the old `has_artifact("trade_decision")` gate meant
    the delta path wrote NO trade_results row at all — measured, 40 of 40
    delta analyses over 21 days, including 5 that executed real orders
    (UNH, ALLY, AXP x2, DIS).

    The docstring on `_persist_policy_action` justified that hole as "a path
    that never produced a trade decision". True of the GLANCE tier, which
    writes a hardcoded HOLD@0 and trades nothing. False of DELTA, which is
    an agent's reasoned BUY/SELL that pipeline_service will execute — so it
    was invisible to P&L, the scorecard, strategy tracking and the judge.
    """
    decision = decision if decision is not None else desk.trade_decision
    if not decision and desk.final_decision:
        # Synthesizer produced no valid decision, but the Board of Directors reached
        # an auditable verdict. Fall back to the Board's decision so the desk's
        # research and verdict are not lost.
        board_dec = desk.final_decision
        decision = {
            "action": board_dec.get("action", "HOLD"),
            "confidence": board_dec.get("confidence", 50),
            "reasoning": str(board_dec.get("reasoning", "")) + " [Synthesizer unavailable; persisted from Board of Directors verdict]",
            "signal_weights": {"quant": 0.25, "fundamental": 0.25, "debate": 0.25, "board": 0.25},
            "signal_assessments": {
                "board": "Board fallback",
                "quant": "Synthesizer fallback",
                "fundamental": "Synthesizer fallback",
                "debate": "Synthesizer fallback",
            },
            "risk_flags": board_dec.get("risk_flags", []),
            "stop_loss": board_dec.get("stop_loss"),
            "take_profit": board_dec.get("take_profit"),
            "position_size_pct": board_dec.get("position_size_pct", 0.0),
            "decision_provenance": "board_fallback",
        }
        desk.trade_decision = decision
        logger.warning(
            "[V3] %s: decision_synthesizer produced no decision, falling back to Board verdict (%s @ %s%%)",
            ticker, decision["action"], decision["confidence"],
        )
    if decision:
        try:
            from app.services.trade_result_saver import save_trade_result
            trade_decision = decision

            # BEFORE the write, so trade_results can never store a level the
            # sanitizer already rejected (it used to run afterwards, inside
            # the policy chain — see _drop_implausible_levels).
            _drop_implausible_levels(desk)

            # The confidence cap that used to live here is GONE (2026-08-03).
            # It rewrote a live decision's `confidence` to 60 while its own
            # comment claimed it was "deliberately NOT the full downgrade to
            # HOLD" — but ANALYSIS_CONFIDENCE_THRESHOLD is 70, so every
            # decision it touched was then blocked as LOW_CONFIDENCE. Two
            # separate harms: the label blamed the desk for a number WE
            # wrote, and no capped trade could ever execute, so the outcome
            # evidence the gate said it was gathering could never arrive
            # (that is why "only 1 of 7 flagged trades has resolved").
            #
            # The dissent is now surfaced to the board BEFORE it decides (see
            # the board_of_directors branch) and enforced, only when left
            # unanswered, by HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT. Whether
            # the agent addressed it is recorded here so the question "does
            # answering the dissent predict a better trade?" stays askable.
            try:
                from app.v3.contradiction_shadow import resolution_is_substantive

                if desk.cycle_metadata.get("dissent_detected"):
                    _merged = {**(desk.final_decision or {}), **trade_decision}
                    trade_decision["dissent_addressed"] = resolution_is_substantive(_merged)
                    trade_decision["dissent_sources"] = (
                        desk.cycle_metadata["dissent_detected"].get("sentiment_by_source")
                    )
            except Exception as gate_err:
                logger.warning("[V3] %s: dissent stamp failed (non-fatal): %s", ticker, gate_err)
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

            # Ensure dynamic trigger extracted from prose or inherited from the board
            # is attached to trade_decision before persisting to trade_results.
            if not isinstance(trade_decision.get("dynamic_trigger"), dict):
                board_decision = desk.final_decision or {}
                dt = (
                    board_decision.get("dynamic_trigger")
                    if isinstance(board_decision.get("dynamic_trigger"), dict)
                    else None
                )
                if not dt:
                    dt = extract_dynamic_trigger_from_text(
                        str(trade_decision.get("reasoning") or "")
                    ) or extract_dynamic_trigger_from_text(
                        str(board_decision.get("reasoning") or "")
                    )
                if isinstance(dt, dict):
                    trade_decision["dynamic_trigger"] = dt

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
                # The model that actually decided: last attributed agent run
                # on this desk (the decision/synthesis agent runs last).
                # "v3_pipeline" survives only as the no-attribution fallback —
                # rows with a real model are what the leaderboard groups on.
                _models = [e.get("model_used") for e in _telemetry if e.get("model_used")]
                _providers = [e.get("provider") for e in _telemetry if e.get("provider")]
                log_rlm_audit_trail(
                    cycle_id=cycle_id,
                    bot_id=bot_id,
                    ticker=ticker,
                    context=desk.get_compressed_context(include_debate=True),
                    trading_system_prompt="V3 pure agentic pipeline (desk-compressed context)",
                    active_model=(_models[-1] if _models else "v3_pipeline"),
                    response_text=json.dumps(trade_decision, default=str),
                    tokens_used=sum(int(e.get("token_usage") or 0) for e in _telemetry),
                    execution_time=sum(int(e.get("elapsed_ms") or 0) for e in _telemetry) / 1000.0,
                    agent_step="v3_decision",
                    # Which box decided, and how much context the desk carried:
                    # per-agent prompt_tokens is the harness's LAST-request
                    # input snapshot, so this sum is the desk's final-request
                    # context mass — 0 here meant box saturation was invisible
                    # from the DB (08-04 audit).
                    endpoint_name=(_providers[-1] if _providers else ""),
                    prompt_tokens=sum(int(e.get("prompt_tokens") or 0) for e in _telemetry),
                )
            except Exception as audit_err:
                logger.warning("[V3] %s: decision audit log failed (non-fatal): %s", ticker, audit_err)

            # Paired challenger (observational): re-decide from the same
            # desk evidence under the experimental spec, log the pair.
            # Only runs when CHALLENGER_SPEC is set — see app/v3/challenger.
            #
            # Full panel only. The challenger re-decides from the desk's
            # research/debate artifacts, and a delta desk carries none of
            # them — pairing a full-evidence challenger against a re-look
            # would compare the spec to a different question.
            try:
                from app.v3.challenger import get_challenger_spec, run_challenger
                if source == "v3_full" and get_challenger_spec():
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


async def run_deep_retrieval_for_synthesizer(
    *,
    ticker: str,
    judge_confidence: int,
    emit: Any,
    build_block: Any = None,
) -> str | None:
    """Decomposed recall for the synthesizer, announced while it runs.

    When the debate judge lands under 60 the synthesizer gets a deep-recall
    block: one small LLM call that splits a fixed question into sub-queries,
    then a handful of hybrid retrievals. Returns the block, or None when the
    gate is shut or the retrieval failed (non-fatal by design — the synthesizer
    runs without it).

    MEASURED 2026-09-05/06. SNOW's board finished at 23:44:50 and the
    synthesizer announced itself at 23:48:05 — **195 seconds** in which the
    pipeline emitted nothing at all and `pipeline_state.progress` still read
    the Board's chat line. LULU, the cycle before: 181 s. The retrievals take
    ~2 s of that; the rest is the decomposition call.

    Nothing could see it. It is not an agent, so there is no
    `v3_agent_telemetry` row, no agent_start/agent_done event and no
    `[V3Runner]` line, and the synthesizer's own "starting..." emit fires only
    after this returns. Over 16 days the board->synthesizer gap has a median of
    0 s on DeepSeek (n=52) and Nemotron (n=31) — the gate mostly stays shut —
    against n=4, median 160 s on GLM, every desk.

    `build_block` is injectable for tests; production uses
    `retrieval_decomposed.build_decomposed_block`.
    """
    if judge_confidence >= 60:
        return None

    if build_block is None:
        from app.services.retrieval_decomposed import build_decomposed_block

        build_block = build_decomposed_block

    emit(
        "analyzing",
        f"v3_deep_retrieval_{ticker}",
        f"🔎 {ticker}: deep retrieval for the synthesizer "
        f"(debate verdict confidence {judge_confidence} < 60)",
        status="running",
        data={"kind": "deep_retrieval_start", "ticker": ticker,
              "judge_confidence": int(judge_confidence)},
    )

    t0 = time.monotonic()
    block: str | None = None
    status = "ok"
    detail_tail = ""
    try:
        block = await build_block(
            ticker,
            f"What are the key risks, catalysts, and conflicting "
            f"signals for {ticker}?",
        )
    except Exception as deep_err:  # noqa: BLE001 — advisory, never blocks a desk
        status = "error"
        detail_tail = f" — failed: {str(deep_err)[:120]}"
        logger.warning(
            "[V3] %s: deep retrieval failed (non-fatal): %s", ticker, deep_err
        )
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if status == "ok" and not block:
        status = "warn"
        detail_tail = " — no chunks"

    emit(
        "analyzing",
        f"v3_deep_retrieval_done_{ticker}",
        f"🔎 {ticker}: deep retrieval finished in {elapsed_ms / 1000:.1f}s"
        f"{detail_tail}",
        status=status,
        data={"kind": "deep_retrieval_done", "ticker": ticker,
              "elapsed_ms": elapsed_ms, "chars": len(block or "")},
    )

    if block:
        logger.info(
            "[V3] %s: deep retrieval injected for synthesizer "
            "(verdict confidence %d, %d chars, %dms)",
            ticker, judge_confidence, len(block), elapsed_ms,
        )
    return block


def _judge_confidence(verdict: Any) -> int:
    """Read a debate verdict's confidence from EITHER artifact shape.

    `debate_judge` has two live writers with incompatible key names:

      * the judge agent itself emits `winner` + `final_confidence` (that is the
        required schema in artifacts.py), and with DEBATE_ENGINE=3 — the
        default — bull/bear/judge IS the live debate path;
      * the tournament copy and the two skip markers written in
        `_queue_debate_phase` emit `winning_side` + `confidence`.

    Readers that knew only one shape silently scored the other as 0.
    `shared_desk.get_compressed_context` and `agent_runner` already accept both;
    the two readers in this module did not, which is why the synthesizer's
    "only when the verdict is low-confidence" deep-retrieval hook fired on every
    real judge verdict, including 18 in 14 days that were actually >= 60.

    Anything unreadable is 0 — the same direction the callers already default to.
    """
    if not isinstance(verdict, dict):
        return 0
    raw = verdict.get("confidence")
    if raw is None:
        raw = verdict.get("final_confidence")
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def _drop_implausible_levels(desk: SharedDesk) -> list[str]:
    """Drop decimal-error stop/target levels off the live decision artifact.

    Stop/target sanity against the last close (2026-07-28 fidelity audit).
    `stop_loss` and `take_profit` are emitted by BOTH the Board and the
    synthesizer and NOTHING checked either — they were the largest unguarded
    surface left, and they size real orders. Measured over 14 days: 3 of 358
    decisions carried an implausible level, including LMT with a stop of $0.92
    and a target of $1.25 against a $581 close. That is not a bad trade, it is a
    decimal error, and executing it would either never stop out or liquidate
    instantly.

    The band is deliberately wide (0.3x-1.5x of close for a stop, 0.7x-3.0x for
    a target). It is a DECIMAL-ERROR detector, not a strategy opinion: a tight
    stop and an ambitious target are both legitimate and must pass.

    Lifted OUT of `_apply_policy_gates` (2026-08-03). It is a sanitizer, not a
    gate — it returns no label and mutates the artifact — and living inside the
    gate chain meant it ran at whatever point that chain happened to be called:

      * full panel — gates ran BEFORE the result was built, so the drop reached
        the executor, but AFTER `save_trade_result` and `save_desk`, so
        `trade_results` and `shared_desk` both kept the bad number while
        telemetry claimed DROPPED_IMPLAUSIBLE_LEVEL;
      * delta re-look — gates ran AFTER `_build_v1_compatible_result`, so the
        dropped level survived in `result["estimate"]["stop_loss"]`, which
        pipeline_service hands straight to `buy()` as a live stop order.

    One sanitizer, called before anything reads or persists the levels, removes
    both. Idempotent: a level already dropped to None fails the isinstance check
    and is skipped, so calling it twice is free.

    Returns the field names it dropped (empty list = nothing to do).
    """
    decision = desk.trade_decision or desk.final_decision or {}
    if not isinstance(decision, dict):
        return []
    action = str(decision.get("action") or "").strip().upper() or "HOLD"

    _levels = {
        "stop_loss": (0.3, 1.5),
        "take_profit": (0.7, 3.0),
    }
    _last_close = None
    try:
        from app.db import mongo_query
        _row = mongo_query.find_row('price_history', {'ticker': desk.ticker}, ['close'], sort=[('date', -1)])
        _last_close = float(_row[0]) if _row and _row[0] else None
    except Exception as _e:  # noqa: BLE001 — a price lookup must never block
        logger.debug("[V3] %s: stop/target sanity lookup failed: %s",
                     desk.ticker, _e)

    dropped: list[str] = []
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
                dropped.append(_field)
                _record_gate(
                    desk, "DROPPED_IMPLAUSIBLE_LEVEL", action=action,
                    field=_field, value=_v, last_close=round(_last_close, 2),
                )
                logger.warning(
                    "[V3] %s: %s=%s is implausible against a %.2f close — "
                    "dropped (band %.1fx-%.1fx)",
                    desk.ticker, _field, _v, _last_close, _lo, _hi,
                )
    return dropped


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

    # Stale-price gate (promoted from shadow 2026-07-31). The desk's whole
    # technical baseline — stops, targets, sizing, fair-value anchor — derives
    # from the pinned price series; when that series is >3 trading days old the
    # numbers are fiction (cycle-v3-1785504601: RBLX priced 24% off, stop above
    # spot). A HOLD on stale data trades nothing and passes above; a BUY/SELL
    # must not execute. Age is stashed at desk-build time by the detection
    # block; absent means fresh (or detection failed — executor checks remain
    # the backstop for that).
    stale_age = _stale_age(desk.cycle_metadata.get("stale_price_age_trading_days"))
    if stale_age is not None and stale_age > 3:
        return _record_gate(
            desk, "HOLD_POLICY_BLOCKED_STALE_PRICE_DATA",
            action=action, age_trading_days=stale_age,
            as_of=desk.cycle_metadata.get("stale_price_as_of"),
        )

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

    # Unresolved cross-desk dissent (2026-08-03). The desks disagreed on
    # direction, the board was TOLD so before it decided, and it is trading
    # anyway without saying why the dissenting desk is wrong.
    #
    # This is the same shape as the jury-veto override directly below: the
    # disagreement does not forbid the trade, but overriding it in silence is
    # not a decision. Fail-CLOSED — a missing resolution blocks, because the
    # whole failure mode being fixed here is a gate that looked like it was
    # merely trimming confidence while actually blocking every trade under a
    # label that blamed the desk.
    #
    # Only reachable when detection ran and found a conflict; an absent
    # `dissent_detected` (detection failed, or the desks agreed) never blocks.
    if desk.cycle_metadata.get("dissent_detected"):
        from app.v3.contradiction_shadow import resolution_is_substantive

        mitigation = {**(desk.final_decision or {}), **(desk.trade_decision or {})}
        if not resolution_is_substantive(mitigation):
            return _record_gate(
                desk, "HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT",
                action=action, confidence=confidence,
                sentiment_by_source=(
                    desk.cycle_metadata["dissent_detected"].get("sentiment_by_source")
                ),
            )

    tournament = getattr(desk, "tournament_result", None) or {}

    # NOTE: `tournament_result` outlived the tournament. It is now written ONLY
    # by the two debate-skip markers in _queue_debate_phase (regime panic, and
    # no-trade-available), both of which set `risk_flags` precisely so the gate
    # below fires. The jury-majority veto that used to read `vetoed` here is
    # gone with the tournament: it blocked ZERO decisions in its lifetime, and
    # since both surviving writers hardcode `"vetoed": False` the branch could
    # not fire again.

    # A standing risk flag: the board may trade through it ONLY with explicit
    # mitigation — a defined stop-loss, a dynamic trigger, and its own reasoned
    # position size. Anything less holds.
    if tournament.get("risk_flags"):
        mitigation = {**(desk.final_decision or {}), **(desk.trade_decision or {})}
        has_stop = isinstance(mitigation.get("stop_loss"), (int, float))
        has_trigger = bool(mitigation.get("dynamic_trigger"))
        has_size = isinstance(mitigation.get("position_size_pct"), (int, float))
        if not (has_stop and has_trigger and has_size):
            return _record_gate(
                desk, "HOLD_POLICY_BLOCKED_UNMITIGATED_RISK",
                action=action, has_stop=has_stop,
                has_trigger=has_trigger, has_size=has_size,
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

    # Record BEFORE checking. Recording used to happen only on the survive
    # path below, so every abort reason read "failed 0 time(s) with outcomes
    # []" — the phase really had failed, the ledger just hadn't seen it
    # (observed on cycle-v3-1787786020 and the 2026-08-05 KSS abort).
    # should_abort() reads only retry counts, so the order change cannot
    # alter which outcomes abort.
    breaker.record_outcome(phase_name, outcome)

    if outcome in (PhaseOutcome.TIMED_OUT,):
        logger.error("[V3] %s: %s TIMED OUT — aborting pipeline", ticker, phase_name)
        desk.advance_phase(DeskPhase.ABORTED, outcome)
        save_desk(desk)
        _page_phase_abort(desk, phase_name, f"{phase_name} timed out")
        return _build_noop_result(desk, reason=f"{phase_name} timed out")

    if breaker.should_abort(phase_name, outcome):
        logger.error("[V3] %s: Circuit breaker tripped on %s — aborting pipeline", ticker, phase_name)
        desk.advance_phase(DeskPhase.ABORTED, outcome)
        save_desk(desk)
        reason = breaker.get_abort_reason(phase_name)
        _page_phase_abort(desk, phase_name, reason)
        return _build_noop_result(desk, reason=reason)

    return None


def _page_phase_abort(desk: SharedDesk, phase_name: str, reason: str) -> None:
    """An aborted desk used to be invisible: a log line plus a HOLD@0 noop row
    that reads like a quiet decision. Page it (deduped, never raises)."""
    try:
        from app.services.degraded_alert import alert_phase_abort

        alert_phase_abort(
            cycle_id=getattr(desk, "cycle_id", "") or "",
            ticker=desk.ticker,
            phase=phase_name,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 — paging must never hurt the abort path
        logger.warning("[V3] phase-abort paging failed (non-fatal): %s", exc)


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
            phase=phase_name,
        )

        # If failed and retryable, try once more
        if outcome not in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP):
            if breaker.should_retry(phase_name, outcome):
                # Ledger the attempt we are retrying away from — otherwise the
                # eventual abort reason undercounts ("failed 1 time" on a
                # phase that failed twice).
                breaker.record_outcome(phase_name, outcome)
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
                    phase=phase_name,
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
            # The held branch used to state the position as a bare FACT while
            # the not-held branch below stated a hard CONSTRAINT. That asymmetry
            # is why the book never sold (2026-08-05): with nothing framing the
            # exit, the desk kept reasoning about entry even on names it owned,
            # and a broken thesis came out as "wait for confirmation before
            # re-engaging" (HOOD, 08-05) — which silently means KEEP. `HOLD`
            # carries both meanings and only the entry one was being reasoned.
            metadata["portfolio_context"] = (
                f"CURRENTLY HOLDING {ticker}: "
                f"Entry ${(pos_ctx.get('avg_entry') or 0):.2f}, "
                f"P&L {(pos_ctx.get('unrealized_pnl_pct') or 0):+.1f}%, "
                f"Held {pos_ctx.get('holding_days', 0)} days.\n"
                "YOU ALREADY OWN THIS. You are not deciding whether to enter — "
                "you are deciding what to do with capital that is already "
                "committed and already at risk. Your three actions mean:\n"
                f"  BUY  = add to the existing {ticker} position\n"
                "  HOLD = KEEP it at its current size, and you are accountable "
                "for that as an active choice\n"
                "  SELL = EXIT it. This is available to you and it is the "
                "CORRECT action when the thesis that opened the position no "
                "longer holds.\n"
                "Judge the position on its thesis, not on its P&L, and not on "
                "whether you would open it again today. 'Wait for confirmation "
                "before re-engaging' is not available here — you are already "
                "engaged, and choosing HOLD is choosing to stay engaged."
            )
            metadata["held"] = True
            # Structured copy for the debate framer, which must not parse prose.
            metadata["position"] = {
                "held": True,
                "avg_entry": pos_ctx.get("avg_entry"),
                "unrealized_pnl_pct": pos_ctx.get("unrealized_pnl_pct"),
                "holding_days": pos_ctx.get("holding_days"),
            }
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



def _stages_completed(desk) -> list[str]:
    """What this desk ACTUALLY reached, from its artifacts and phase.

    This used to be a hardcoded four-element list, so a desk stuck at INIT with
    only a junior-analyst note claimed research, debate and decision had all
    run. Three tickers per wide cycle were lost that way and every downstream
    reader — reports, audits, the reconciliation check — believed them.
    """
    stages: list[str] = []
    have = {a.get("artifact_type") for a in (getattr(desk, "artifacts", None) or [])
            if isinstance(a, dict)}
    if "regime_classification" in have:
        stages.append("regime_classification")
    if have & {"fundamental_report", "quant_report", "valuation_report"}:
        stages.append("research")
    if have & {"bull_argument", "bear_rebuttal", "bull_defense", "debate_judge"}:
        stages.append("debate")
    decision = next((a for a in (getattr(desk, "artifacts", None) or [])
                     if isinstance(a, dict) and a.get("artifact_type") == "final_decision"), None)
    # A DEGRADED sentinel is not a decision — it is the record of not making one.
    if decision and (decision.get("content") or {}).get("action") is not None:
        stages.append("decision")
    return stages


def extract_dynamic_trigger_from_text(text: str) -> dict[str, Any] | None:
    """Extract an intended dynamic_trigger specification declared in prose.

    When an LLM mentions a dynamic trigger in narrative reasoning (e.g.
    "Dynamic trigger sma_50_drop at $209.28 set as watch level for thesis reassessment.")
    but forgets to emit the structured JSON key `dynamic_trigger`, this helper
    parses the setup and level, validates it against the order triggers vocabulary,
    and constructs the structured dict.
    """
    if not text or not isinstance(text, str):
        return None

    from app.trading.order_triggers import (
        dynamic_trigger_is_evaluable,
        normalize_dynamic_trigger_type,
    )

    # Pattern 1: (dynamic trigger|dynamic_trigger) [optional separator] <type> [at|@|level] $<value>
    m = re.search(
        r"(?:dynamic\s*trigger|dynamic_trigger)[\s:=]+([a-zA-Z0-9_]+)[\s,]+(?:at|@|level|of)?\s*\$?([0-9]+(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE,
    )
    if m:
        raw_type = m.group(1).lower()
        try:
            raw_val = float(m.group(2))
            normalized = normalize_dynamic_trigger_type(raw_type)
            if dynamic_trigger_is_evaluable(normalized):
                return {"type": normalized, "value": raw_val}
        except (ValueError, TypeError):
            pass

    # Pattern 2: setting dynamic trigger <type> at $<value>
    m2 = re.search(
        r"(?:dynamic\s*trigger)[\s]+(?:of\s+)?([a-zA-Z0-9_]+)\s+(?:at|@|level|of)?\s*\$?([0-9]+(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE,
    )
    if m2:
        raw_type = m2.group(1).lower()
        try:
            raw_val = float(m2.group(2))
            normalized = normalize_dynamic_trigger_type(raw_type)
            if dynamic_trigger_is_evaluable(normalized):
                return {"type": normalized, "value": raw_val}
        except (ValueError, TypeError):
            pass

    return None


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
    # Execution parameters: prefer trade_decision overrides, but inherit
    # Board levels and dynamic_trigger so that conditional entry orders and
    # risk bounds survive even if the synthesizer focused purely on synthesis.
    _merged = {**(desk.final_decision or {}), **(desk.trade_decision or {})}
    stop_loss = (desk.trade_decision or {}).get("stop_loss") or (desk.final_decision or {}).get("stop_loss")
    take_profit = (desk.trade_decision or {}).get("take_profit") or (desk.final_decision or {}).get("take_profit")
    dynamic_trigger = (desk.trade_decision or {}).get("dynamic_trigger") or (desk.final_decision or {}).get("dynamic_trigger")
    if not dynamic_trigger or not isinstance(dynamic_trigger, dict):
        # Fallback: extract dynamic trigger declared in prose reasoning
        extracted = extract_dynamic_trigger_from_text(rationale) or extract_dynamic_trigger_from_text((desk.final_decision or {}).get("reasoning", ""))
        if extracted:
            logger.info("[V3] %s: extracted dynamic_trigger from reasoning prose: %s", desk.ticker, extracted)
            dynamic_trigger = extracted
    exit_style = (desk.trade_decision or {}).get("exit_style") or (desk.final_decision or {}).get("exit_style")
    # Sizing is situational: the board reasons about position_size_pct; the
    # synthesizer may override it. Execution honors this over any formula.
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
    # "passed" is a VERDICT, and a debate that never happened has none.
    # Defaulting to it meant a ticker lost before any analyst ran still
    # reported a clean integrity check to every downstream reader.
    _debated = "debate" in _stages_completed(desk)
    v2_debate = {
        "judge_action": action,
        "judge_confidence": confidence,
        "winning_side": "split" if _debated else "none",
        "integrity_status": "passed" if _debated else "not_run",
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
        # Not "always": a desk that never dispatched an analyst did not escalate.
        "escalated": bool(_stages_completed(desk)) and "research" in _stages_completed(desk),
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
            "stages_completed": _stages_completed(desk),
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
        # Both shapes, same reason as _judge_confidence: a validator-coerced
        # judge artifact carries `winning_side`/`confidence` instead of
        # `winner`/`final_confidence`, and reading only the latter scored it
        # "tie" at 0 — a real verdict rendered as no verdict.
        winner = desk.debate_judge.get("winner") or desk.debate_judge.get("winning_side") or "tie"
        conf = _judge_confidence(desk.debate_judge)
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
