"""
Debate Coordinator V3 — Family Office Dynamic Debate System.

Replaces the rigid N-turn sequential debate with a CIO-driven dynamic
loop. The CIO actively controls debate flow, rejects empty claims,
and demands more data until rigorous evidence exists — or until the
max-loop guardrail fires.

Architecture:
  1. Memory PM injects Brain Graph context (pre-analysis)
  2. 5 PMs analyze evidence in parallel (Fundamental, Growth, Macro, Risk, Memory)
  3. Cross-Examiner verifies all claims against evidence
  4. CIO evaluates: NEEDS_MORE_DATA or READY_FOR_VERDICT
  5. If NEEDS_MORE_DATA: Workers fetch data → PMs re-analyze → loop to step 3
  6. If READY_FOR_VERDICT: CIO produces final verdict
  7. Max 3 loops → forced verdict or ABSTAIN

All LLM calls go through app.services.vllm_client (Rule 2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.cognition.contracts.family_office import (
    CIODirective,
    CIODirectiveStatus,
    DebateRound,
    FamilyOfficeResult,
    FamilyOfficeVerdict,
    ManagerArgument,
    ManagerRole,
)
from app.cognition.contracts.debate import DebateResult
from app.services.adaptive_concurrency import concurrency_controller
from app.services.logging.tracer import trace_span

logger = logging.getLogger(__name__)


# ── Which PMs run in the initial parallel analysis ──────────────────────
ANALYSIS_PMS = [
    ManagerRole.IMHOTEP,
    ManagerRole.PYTHAGORAS,
    ManagerRole.ARCHIMEDES,
    ManagerRole.CAESAR,
    ManagerRole.AL_KHWARIZMI,
    ManagerRole.BRAHMAGUPTA,
    ManagerRole.NEWTON_LEIBNIZ,
]


async def _get_memory_context(ticker: str, cycle_id: str) -> str:
    """Fetch Brain Graph / historical memory for this ticker.

    Queries prior trade outcomes, episodic memory, and procedural
    rules relevant to this ticker. Non-blocking — returns empty
    string on any failure.
    """
    parts = []

    # Prior trade outcomes
    try:
        from app.agents.base_agent import get_ticker_outcome_context
        outcome_ctx = get_ticker_outcome_context(ticker)
        if outcome_ctx:
            parts.append(outcome_ctx)
    except Exception:
        pass

    # Episodic memory from Brain Graph
    try:
        from app.cognition.memory.reader import read_episodic_memory
        episodes = await read_episodic_memory(ticker, limit=5)
        if episodes:
            parts.append("## EPISODIC MEMORY (Prior Analyses):")
            for ep in episodes:
                if isinstance(ep, dict):
                    parts.append(
                        f"- [{ep.get('timestamp', '?')}] "
                        f"{ep.get('action', '?')} @ {ep.get('confidence', '?')}% — "
                        f"{ep.get('rationale', '')[:200]}"
                    )
    except Exception:
        pass

    # Procedural rules (trading constitution)
    try:
        from app.pipeline.trading_constitution import get_constitution_rules
        rules = get_constitution_rules()
        if rules:
            parts.append(f"## PROCEDURAL RULES:\n{rules[:1000]}")
    except Exception:
        pass

    return "\n\n".join(parts) if parts else ""


def _format_worker_results(worker_results: list) -> str:
    """Format worker results into readable text for PM prompts."""
    if not worker_results:
        return ""

    lines = []
    for wr in worker_results:
        if wr.success and wr.data:
            lines.append(
                f"### [{wr.worker_type.value}] {wr.request_description}\n"
                f"{wr.data[:2000]}"
            )
        elif not wr.success:
            lines.append(
                f"### [{wr.worker_type.value}] {wr.request_description}\n"
                f"⚠️ FAILED: {wr.error}"
            )
    return "\n\n".join(lines)


def _build_round_summary(rnd: DebateRound) -> str:
    """Build a text summary of a debate round for injection into next round."""
    parts = [f"## Round {rnd.round_number} Summary:"]
    for arg in rnd.pm_arguments:
        parts.append(
            f"### {arg.role.value.upper()} (conf: {arg.confidence}%, conv: {arg.conviction})\n"
            f"Key: {arg.key_argument}\n"
            f"Claims: {len(arg.claims)}"
        )
    if rnd.cross_exam_findings:
        parts.append(f"### Cross-Exam:\n{rnd.cross_exam_findings[:500]}")
    if rnd.cio_directive:
        parts.append(
            f"### CIO Directive: {rnd.cio_directive.status.value}\n"
            f"{rnd.cio_directive.rationale}"
        )
    return "\n".join(parts)


@trace_span("debate_coordinator_v3.run_family_office_debate")
async def run_family_office_debate(
    ticker: str,
    packet: "EvidencePacket",
    cycle_id: str = "",
    bot_id: str = "",
    agent_insights: dict[str, str] | None = None,
    position_context: dict | None = None,
    portfolio_dashboard: str = "",
    ctx: "PipelineContext | None" = None,
) -> DebateResult | None:
    """Run the full V3 Family Office debate pipeline.

    Returns a DebateResult (backward compatible with V2) or None
    if the debate is disabled or fails entirely.
    """
    from app.config.config_cognition import cognition_settings
    from app.cognition.debate.action_gate import gate_action
    from app.agents.debate_agents.family_office_managers import (
        run_manager_analysis,
        run_cross_examination,
        run_cio_evaluation,
    )
    from app.agents.debate_agents.family_office_workers import (
        dispatch_workers_parallel,
    )

    held = position_context.get("held", False) if position_context else False
    debate_mode = "HOLD-vs-SELL" if held else "BUY-vs-SELL"
    max_rounds = int(getattr(cognition_settings, "V3_MAX_CIO_LOOPS", 3))
    abstain_on_max = bool(getattr(cognition_settings, "V3_ABSTAIN_ON_MAX_LOOPS", True))
    pm_timeout = float(getattr(cognition_settings, "V3_PM_TIMEOUT_SECONDS", 120))

    logger.info("[V3] " + "═" * 50)
    logger.info("[V3] Starting Family Office %s debate for %s (max %d rounds)",
                debate_mode, ticker, max_rounds)

    t_start = time.monotonic()
    debate_rounds: list[DebateRound] = []
    total_tokens = 0

    # ── Step 0: Memory Context Injection ─────────────────────────────────
    memory_context = ""
    try:
        memory_context = await asyncio.wait_for(
            _get_memory_context(ticker, cycle_id),
            timeout=15.0,  # Memory lookup shouldn't take long
        )
        if memory_context:
            logger.info("[V3] Memory context injected for %s: %d chars", ticker, len(memory_context))
    except asyncio.TimeoutError:
        logger.warning("[V3] Memory context lookup timed out for %s", ticker)
    except Exception as mem_err:
        logger.warning("[V3] Memory context lookup failed for %s: %s", ticker, mem_err)

    # ── Dynamic Debate Loop ──────────────────────────────────────────────
    prior_round_summary = ""
    worker_results_text = ""
    final_verdict: FamilyOfficeVerdict | None = None

    for round_num in range(1, max_rounds + 1):
        logger.info("[V3] === Round %d/%d for %s ===", round_num, max_rounds, ticker)
        t_round = time.monotonic()

        if ctx:
            ctx.safe_emit(
                "analyzing", f"v3_round_{round_num}_{ticker}",
                f"🏛️ {ticker}: Family Office Round {round_num}/{max_rounds}",
                status="running",
            )

        # ── Step 1: Parallel PM Analysis ─────────────────────────────────
        # On first round, all PMs run. On subsequent rounds, only PMs
        # directed by the CIO re-analyze (with new worker data).
        pms_to_run = ANALYSIS_PMS
        if round_num > 1 and debate_rounds:
            last_directive = debate_rounds[-1].cio_directive
            if last_directive and last_directive.directed_managers:
                pms_to_run = last_directive.directed_managers
                logger.info("[V3] Round %d: CIO directed %s to re-analyze",
                            round_num, [p.value for p in pms_to_run])

        async def _run_pm_with_timeout(role: ManagerRole) -> ManagerArgument:
            try:
                return await asyncio.wait_for(
                    run_manager_analysis(
                        role=role,
                        ticker=ticker,
                        packet=packet,
                        cycle_id=cycle_id,
                        bot_id=bot_id,
                        position_context=position_context,
                        portfolio_dashboard=portfolio_dashboard,
                        memory_context=memory_context,
                        prior_round_summary=prior_round_summary,
                        worker_results_text=worker_results_text,
                    ),
                    timeout=pm_timeout,
                )
            except asyncio.TimeoutError:
                logger.error("[V3] PM %s timed out for %s", role.value, ticker)
                return ManagerArgument(role=role)
            except Exception as pm_err:
                logger.error("[V3] PM %s failed for %s: %s", role.value, ticker, pm_err)
                return ManagerArgument(role=role, raw_response=str(pm_err))

        pm_tasks = [_run_pm_with_timeout(role) for role in pms_to_run]
        pm_results = await concurrency_controller.gather(
            pm_tasks, label="v3_pm_analysis", return_exceptions=True,
        )

        pm_arguments: list[ManagerArgument] = []
        round_tokens = 0
        for r in pm_results:
            if isinstance(r, BaseException):
                logger.error("[V3] PM task failed: %s", r)
            elif isinstance(r, ManagerArgument):
                pm_arguments.append(r)
                round_tokens += r.tokens_used

        # If re-analyzing, merge with prior round's non-re-analyzed PMs
        if round_num > 1 and debate_rounds:
            prior_args = {a.role: a for a in debate_rounds[-1].pm_arguments}
            current_roles = {a.role for a in pm_arguments}
            for role, arg in prior_args.items():
                if role not in current_roles:
                    pm_arguments.append(arg)

        logger.info(
            "[V3] Round %d: %d PMs produced %d total claims",
            round_num, len(pm_arguments),
            sum(len(a.claims) for a in pm_arguments),
        )

        # ── Step 2: Cross-Examination ────────────────────────────────────
        cross_findings = ""
        try:
            cross_findings = await asyncio.wait_for(
                run_cross_examination(
                    pm_arguments=pm_arguments,
                    ticker=ticker,
                    packet=packet,
                    cycle_id=cycle_id,
                    bot_id=bot_id,
                ),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[V3] Cross-examination timed out for %s round %d", ticker, round_num)
            cross_findings = "Cross-examination timed out."
        except Exception as cx_err:
            logger.warning("[V3] Cross-examination failed for %s: %s", ticker, cx_err)
            cross_findings = f"Cross-examination failed: {cx_err}"

        # ── Step 3: CIO Evaluation ───────────────────────────────────────
        cio_result = await run_cio_evaluation(
            pm_arguments=pm_arguments,
            cross_exam_findings=cross_findings,
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            round_number=round_num,
            max_rounds=max_rounds,
            held=held,
            position_context=position_context,
        )

        # Build the round record
        cio_directive = None
        if isinstance(cio_result, CIODirective):
            cio_directive = cio_result
            round_tokens += 0  # CIO tokens tracked separately
        elif isinstance(cio_result, FamilyOfficeVerdict):
            final_verdict = cio_result
            round_tokens += cio_result.tokens_used

        elapsed_round = int((time.monotonic() - t_round) * 1000)

        rnd = DebateRound(
            round_number=round_num,
            pm_arguments=pm_arguments,
            cross_exam_findings=cross_findings,
            cio_directive=cio_directive,
            tokens_used=round_tokens,
            elapsed_ms=elapsed_round,
        )
        debate_rounds.append(rnd)
        total_tokens += round_tokens

        # ── If CIO rendered a verdict, we're done ────────────────────────
        if final_verdict:
            logger.info(
                "[V3] CIO rendered verdict in round %d: %s @ %d%%",
                round_num, final_verdict.action, final_verdict.confidence,
            )
            if ctx:
                emoji = "🟢" if final_verdict.action == "BUY" else "🔴" if final_verdict.action == "SELL" else "🟡"
                ctx.safe_emit(
                    "analyzing", f"v3_verdict_{ticker}",
                    f"{emoji} {ticker}: V3 verdict — {final_verdict.action} @ "
                    f"{final_verdict.confidence}% (round {round_num}/{max_rounds})",
                    status="ok",
                )
            break

        # ── CIO wants more data — dispatch workers ───────────────────────
        if cio_directive and cio_directive.status == CIODirectiveStatus.NEEDS_MORE_DATA:
            logger.info(
                "[V3] CIO NEEDS_MORE_DATA in round %d: %d requests, directing %s",
                round_num,
                len(cio_directive.data_requests),
                [m.value for m in cio_directive.directed_managers],
            )

            if ctx:
                ctx.safe_emit(
                    "analyzing", f"v3_more_data_{ticker}",
                    f"📊 {ticker}: CIO requesting more data (round {round_num})",
                    status="running",
                )

            # Collect all data requests (CIO's + PMs')
            all_requests = list(cio_directive.data_requests)
            for arg in pm_arguments:
                for dr in arg.data_requests:
                    if dr.priority == "critical":
                        all_requests.append(dr)

            # Dispatch workers
            worker_results = await dispatch_workers_parallel(
                all_requests, cycle_id, bot_id,
            )

            # Update round with worker results
            rnd_with_workers = rnd.model_copy(update={"worker_results": worker_results})
            debate_rounds[-1] = rnd_with_workers

            # Prepare context for next round
            prior_round_summary = _build_round_summary(rnd_with_workers)
            worker_results_text = _format_worker_results(worker_results)

    # ── Post-Loop: Handle max rounds reached ─────────────────────────────
    max_rounds_reached = final_verdict is None

    if max_rounds_reached:
        logger.warning("[V3] Max rounds reached for %s. Forcing verdict.", ticker)

        if abstain_on_max:
            # ABSTAIN — conservative, no trade
            default_action = gate_action("HOLD", held)
            final_verdict = FamilyOfficeVerdict(
                action=default_action,
                confidence=0,
                winning_side="split",
                rationale=(
                    f"[ABSTAIN] CIO could not reach sufficient evidence quality "
                    f"after {max_rounds} rounds. Defaulting to {default_action}."
                ),
                conviction="WATCH",
            )
        else:
            # Force the CIO to decide on partial data (already handled by
            # the is_final_round flag in run_cio_evaluation)
            pass

    elapsed_total = time.monotonic() - t_start

    # ── Build FamilyOfficeResult ─────────────────────────────────────────
    # Compute per-manager outcomes
    manager_outcomes = {}
    for rnd in debate_rounds:
        for arg in rnd.pm_arguments:
            role_key = arg.role.value
            if role_key not in manager_outcomes:
                manager_outcomes[role_key] = {
                    "claims_count": 0,
                    "confidence": 0,
                    "conviction": "",
                    "key_argument": "",
                    "direction": "neutral",
                }
            manager_outcomes[role_key]["claims_count"] += len(arg.claims)
            manager_outcomes[role_key]["confidence"] = arg.confidence
            manager_outcomes[role_key]["conviction"] = arg.conviction
            manager_outcomes[role_key]["key_argument"] = arg.key_argument
            manager_outcomes[role_key]["direction"] = arg.direction

    fo_result = FamilyOfficeResult(
        ticker=ticker,
        debate_rounds=debate_rounds,
        verdict=final_verdict,
        memory_context_injected=memory_context[:500] if memory_context else "",
        integrity_status="HIGH" if not max_rounds_reached else "LOW_INTEGRITY",
        total_tokens=total_tokens,
        total_rounds=len(debate_rounds),
        max_rounds_reached=max_rounds_reached,
        manager_outcomes=manager_outcomes,
    )

    # ── Convert to DebateResult for backward compat ──────────────────────
    debate_result = fo_result.to_debate_result()

    # ── Audit Logging ────────────────────────────────────────────────────
    try:
        import json as _json
        from pathlib import Path

        _audit_dir = Path("logs/audit")
        _audit_dir.mkdir(parents=True, exist_ok=True)
        run_time = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        cycle_suffix = f"_{cycle_id}" if cycle_id else ""
        log_filename = f"v3_debate_audit_{ticker}{cycle_suffix}_{run_time}.jsonl"

        audit_entry = {
            "version": "v3",
            "ticker": ticker,
            "cycle_id": cycle_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "debate_mode": debate_mode,
            "total_rounds": len(debate_rounds),
            "max_rounds_reached": max_rounds_reached,
            "verdict": {
                "action": final_verdict.action if final_verdict else "NONE",
                "confidence": final_verdict.confidence if final_verdict else 0,
                "winning_side": final_verdict.winning_side if final_verdict else "split",
                "conviction": final_verdict.conviction if final_verdict else "",
                "rationale": final_verdict.rationale if final_verdict else "",
            },
            "tokens": {
                "total": total_tokens,
                "per_round": [r.tokens_used for r in debate_rounds],
            },
            "manager_outcomes": manager_outcomes,
            "elapsed_ms": int(elapsed_total * 1000),
        }

        with open(_audit_dir / log_filename, "w", encoding="utf-8") as f:
            f.write(_json.dumps(audit_entry, indent=2) + "\n")
    except Exception as audit_err:
        logger.error("[V3] Failed to write debate audit: %s", audit_err)

    # ── DB Logging ───────────────────────────────────────────────────────
    try:
        from app.db.connection import get_db
        import json
        import uuid as _uuid

        with get_db() as db:
            db.execute(
                """
                INSERT INTO debate_history
                (id, ticker, cycle_id, pro_argument, con_argument, winner, final_action, final_confidence, persona_outcomes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, cycle_id) DO UPDATE SET
                pro_argument = EXCLUDED.pro_argument,
                con_argument = EXCLUDED.con_argument,
                winner = EXCLUDED.winner,
                final_action = EXCLUDED.final_action,
                final_confidence = EXCLUDED.final_confidence,
                persona_outcomes = EXCLUDED.persona_outcomes
                """,
                [
                    f"dh-v3-{_uuid.uuid4().hex[:12]}",
                    ticker,
                    cycle_id or "manual",
                    json.dumps([c["claim"] for c in debate_result.verified_bull_claims]),
                    json.dumps([c["claim"] for c in debate_result.verified_bear_claims]),
                    debate_result.winning_side,
                    debate_result.judge_action,
                    debate_result.judge_confidence,
                    json.dumps(manager_outcomes),
                ],
            )
    except Exception as db_err:
        logger.error("[V3] Failed to log debate history: %s", db_err)

    try:
        from app.db.mongo import get_mongo_db
        mongo_db = get_mongo_db()
        mongo_db["debate_transcripts"].update_one(
            {"ticker": ticker, "cycle_id": cycle_id or "manual"},
            {
                "$set": {
                    "ticker": ticker,
                    "cycle_id": cycle_id or "manual",
                    "timestamp": datetime.now(timezone.utc),
                    "verdict": {
                        "action": final_verdict.action if final_verdict else "NONE",
                        "confidence": final_verdict.confidence if final_verdict else 0,
                        "winning_side": final_verdict.winning_side if final_verdict else "split",
                        "conviction": final_verdict.conviction if final_verdict else "",
                        "rationale": final_verdict.rationale if final_verdict else "",
                    },
                    "manager_outcomes": manager_outcomes,
                    "total_tokens": total_tokens,
                    "total_rounds": len(debate_rounds)
                }
            },
            upsert=True
        )
        logger.info("[V3] Persisted debate transcript to MongoDB")
    except Exception as mongo_err:
        logger.error("[V3] Failed to log debate transcript to MongoDB: %s", mongo_err)

    logger.info(
        "[V3] VERDICT: %s @ %d%% | Rounds: %d | Tokens: %d | Time: %.1fs",
        debate_result.judge_action,
        debate_result.judge_confidence,
        len(debate_rounds),
        total_tokens,
        elapsed_total,
    )
    logger.info("[V3] " + "═" * 50)

    return debate_result
