"""
Step: MetaOrchestrator Agent Routing + Team Findings.

Dispatches specialist agents (sentiment, fundamental, macro_risk, etc.)
via MetaOrchestrator, then pulls team findings from the TaskBoard.

Extracted from runner.py Steps 5.5, 5.6, 5.65.
"""

import asyncio
import logging
import time

from app.ticker_pipeline.context import TickerContext
from app.log_manager import log_manager

logger = logging.getLogger(__name__)


async def run_agents_step(ctx: TickerContext) -> TickerContext:
    """Run MetaOrchestrator agent routing and pull team findings."""
    from app.cognition.orchestration.meta_orchestrator import MetaOrchestrator
    from app.cycle.orchestration.cycle_control import cycle_control

    ctx.safe_emit(
        "analyzing", f"v2_orchestrator_{ctx.ticker}",
        f"{ctx.ticker}: MetaOrchestrator determining sub-agent routing...",
        status="running",
    )
    await cycle_control.wait_if_paused()
    t_orch = time.monotonic()

    try:
        agent_insights, orch_tokens = await asyncio.wait_for(
            MetaOrchestrator.orchestrate(
                ctx.ticker, ctx.packet, ctx.sufficiency,
                ctx.cycle_id, ctx.bot_id, ctx.is_highly_redundant,
                research_focus=ctx.research_focus,
            ),
            timeout=900.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[V2] MetaOrchestrator TIMEOUT for %s (900s) — injecting fallback context",
            ctx.ticker,
        )
        
        fallback_text = (
            "# ORCHESTRATOR TIMEOUT — PARTIAL EVIDENCE MODE\n"
            "The specialist agent orchestrator timed out. You MUST base your analysis "
            "on the structured facts in the evidence packet, along with any partial "
            "specialist team findings below.\n"
            "Do NOT output zero claims or zero confidence."
        )

        try:
            from app.agents.task_board import task_board
            partial_findings = await task_board.get_findings(
                ticker=ctx.ticker, cycle_id=ctx.cycle_id,
            )
            if partial_findings:
                finding_lines = []
                for f in partial_findings[:10]:
                    src = f.get("source_agent", "?")
                    cat = f.get("category", "fact")
                    content = f.get("content", "")[:200]
                    conf = f.get("confidence", 0)
                    finding_lines.append(f"- [{cat.upper()}] ({src}, conf={conf}): {content}")
                
                fallback_text += (
                    "\n\n## PARTIAL TEAM FINDINGS (from before timeout)\n"
                    + "\n".join(finding_lines)
                )
                logger.info("[V2] Injected %d partial findings during timeout", len(partial_findings))
        except Exception as err:
            logger.debug("Failed to get partial findings during timeout: %s", err)

        agent_insights = {
            "orchestrator_fallback": fallback_text,
        }
        orch_tokens = 0
        ctx.safe_emit(
            "analyzing", f"v2_orchestrator_timeout_{ctx.ticker}",
            f"{ctx.ticker}: MetaOrchestrator TIMEOUT — fallback context injected",
            status="warning",
        )

    ctx.agent_insights = agent_insights or {}
    ctx.add_tokens(orch_tokens)
    ms_orch = ctx.elapsed_ms(t_orch)
    ctx.add_stage("meta_orchestration")
    ctx.orchestrator_had_agents = bool(agent_insights) and not agent_insights.get("orchestrator_fallback")

    log_manager.log_v2_cycle(ctx.cycle_id, "v2_meta_orchestration", {
        "ticker": ctx.ticker,
        "agent_count": len(ctx.agent_insights),
        "agent_keys": list(ctx.agent_insights.keys()),
        "tokens": orch_tokens, "elapsed_ms": ms_orch,
        "fallback": not ctx.orchestrator_had_agents,
    })

    if ctx.agent_insights:
        ctx.safe_emit(
            "analyzing", f"v2_orchestrator_done_{ctx.ticker}",
            f"{ctx.ticker}: MetaOrchestrator completed {len(ctx.agent_insights)} specialist agents",
            elapsed_ms=ms_orch,
        )

    # ── Position context logging ──
    if ctx.held:
        try:
            ctx.safe_emit(
                "analyzing", f"v2_position_{ctx.ticker}",
                f"{ctx.ticker}: Bot HOLDS position — "
                f"entry=${ctx.position_context['avg_entry']}, "
                f"P&L={ctx.position_context['unrealized_pnl_pct']:+.1f}%, "
                f"held {ctx.position_context['holding_days']}d. "
                f"Debate will include sell-thesis framing.",
                status="ok",
            )
        except Exception:
            pass

    # ── Team Findings from TaskBoard ──
    try:
        from app.agents.task_board import task_board

        findings = await task_board.get_findings(
            ticker=ctx.ticker, cycle_id=ctx.cycle_id,
        )
        if findings:
            finding_lines = []
            for f in findings[:10]:
                src = f.get("source_agent", "?")
                cat = f.get("category", "fact")
                content = f.get("content", "")[:200]
                conf = f.get("confidence", 0)
                finding_lines.append(
                    f"- [{cat.upper()}] ({src}, conf={conf}): {content}"
                )
            ctx.team_findings_summary = "\n".join(finding_lines)
            ctx.agent_insights["team_findings"] = (
                f"# TEAM FINDINGS FROM SPECIALIST AGENTS\n"
                f"{len(findings)} findings shared by team:\n"
                f"{ctx.team_findings_summary}"
            )
            logger.info(
                "[V2] [COLLAB] Injected %d team findings for %s",
                len(findings), ctx.ticker,
            )
            ctx.safe_emit(
                "analyzing", f"v2_team_findings_{ctx.ticker}",
                f"{ctx.ticker}: {len(findings)} team findings injected into debate context",
                status="ok",
            )
    except Exception as tb_err:
        logger.debug("[V2] TaskBoard read failed for %s: %s", ctx.ticker, tb_err)

    # ── Emit Agent Voice Quotes for run agents ──
    try:
        from app.services.agent_voice_service import dispatch_agent_quote
        for label, insight in ctx.agent_insights.items():
            if not insight or str(insight).startswith("Failed") or str(insight).startswith("Error"):
                continue
            
            if label == "data_integrity":
                dispatch_agent_quote(
                    agent_id="DATA_JANITOR_AGENT",
                    archetype="DATA_JANITOR",
                    context={
                        "ticker": ctx.ticker,
                        "cycle_id": ctx.cycle_id,
                        "tool": "data_integrity_check",
                        "action_result": "data_health",
                        "agent_insight": insight,
                    }
                )
            elif label == "sentiment":
                insight_lower = str(insight).lower()
                archetype = "BULL" if "bullish" in insight_lower else "BEAR" if "bearish" in insight_lower else "QUANT"
                dispatch_agent_quote(
                    agent_id="SENTIMENT_AGENT",
                    archetype=archetype,
                    context={
                        "ticker": ctx.ticker,
                        "cycle_id": ctx.cycle_id,
                        "tool": "sentiment_analysis",
                        "action_result": archetype,
                        "agent_insight": insight,
                    }
                )
            elif label == "fundamentals":
                dispatch_agent_quote(
                    agent_id="FUNDAMENTAL_AGENT",
                    archetype="RESEARCH",
                    context={
                        "ticker": ctx.ticker,
                        "cycle_id": ctx.cycle_id,
                        "tool": "fundamental_analysis",
                        "action_result": "synthesis",
                        "agent_insight": insight,
                    }
                )
            elif label == "quant_critique":
                dispatch_agent_quote(
                    agent_id="QUANT_CRITIQUE_AGENT",
                    archetype="QUANT",
                    context={
                        "ticker": ctx.ticker,
                        "cycle_id": ctx.cycle_id,
                        "tool": "quantitative_critique",
                        "action_result": "critique",
                        "agent_insight": insight,
                    }
                )
            elif label == "macro_risk":
                dispatch_agent_quote(
                    agent_id="MACRO_RISK_AGENT",
                    archetype="RISK",
                    context={
                        "ticker": ctx.ticker,
                        "cycle_id": ctx.cycle_id,
                        "tool": "macro_risk_analysis",
                        "action_result": "anxious",
                        "agent_insight": insight,
                    }
                )
            elif label == "deep_research":
                dispatch_agent_quote(
                    agent_id="DEEP_RESEARCH_AGENT",
                    archetype="QUANT",
                    context={
                        "ticker": ctx.ticker,
                        "cycle_id": ctx.cycle_id,
                        "tool": "deep_research",
                        "action_result": "academic",
                        "agent_insight": insight,
                    }
                )
    except Exception as voice_err:
        logger.debug("Voice event trigger failed: %s", voice_err)

    return ctx

