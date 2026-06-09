import logging
import asyncio
from typing import Dict

from app.cognition.contracts.evidence import EvidencePacket
from app.cognition.verification.sufficiency_gate import SufficiencyResult
from app.agents.debate_agents.specialized_agents import (
    analyze_sentiment,
    analyze_macro_risk,
    analyze_fundamentals,
    analyze_deep_research,
)

logger = logging.getLogger(__name__)


from app.services.adaptive_concurrency import concurrency_controller

class MetaOrchestrator:
    """
    Deterministically routes to specialized agents depending on the EvidencePacket
    and Data Sufficiency.
    """

    @staticmethod
    async def orchestrate(
        entity_id: str,
        packet: EvidencePacket,
        sufficiency: SufficiencyResult,
        cycle_id: str,
        bot_id: str,
        is_highly_redundant: bool = False,
        research_focus: str = "",
    ) -> tuple[Dict[str, str], int]:
        """
        Rule-based router. Dispatches specialized sub-agents based on evidence health in a staggered sequence.
        Returns a dict of agent_name -> insight, and total tokens used.
        """
        results = {}
        total_tokens = 0
        PER_AGENT_TIMEOUT = 600.0

        # ── STAGE 1: Fundamentals and Sentiment ──
        stage1_tasks = []
        stage1_labels = []

        # Rule 2: If sentiment features are intact, run sentiment agent
        if (
            "sentiment" not in packet.missing_fields
            and "news" not in packet.missing_fields
        ):
            stage1_tasks.append(analyze_sentiment(entity_id, packet, cycle_id, bot_id, research_focus))
            stage1_labels.append("sentiment")

        # Rule 3: Run Fundamental agent if basic financials exist
        if (
            "fundamentals" not in packet.missing_fields
            and "pe_ratio" not in packet.missing_fields
        ):
            stage1_tasks.append(analyze_fundamentals(entity_id, packet, cycle_id, bot_id, research_focus))
            stage1_labels.append("fundamentals")

        if stage1_tasks:
            logger.info(f"[{entity_id}] MetaOrchestrator Stage 1: Running {stage1_labels}")
            try:
                wrapped_tasks = [
                    asyncio.wait_for(task, timeout=PER_AGENT_TIMEOUT)
                    for task in stage1_tasks
                ]
                outputs = await concurrency_controller.gather(wrapped_tasks, label="meta_orchestrator_stage1", return_exceptions=True)
                for label, out in zip(stage1_labels, outputs):
                    if isinstance(out, asyncio.TimeoutError):
                        logger.warning(f"[{entity_id}] MetaOrchestrator: {label} TIMED OUT")
                        results[label] = f"Error: Agent timed out"
                    elif isinstance(out, Exception):
                        logger.error(f"[{entity_id}] MetaOrchestrator task {label} failed: {out}")
                        results[label] = f"Error: {out}"
                    else:
                        results[label] = out[0]
                        total_tokens += out[1]
            except Exception as e:
                logger.error(f"[{entity_id}] MetaOrchestrator Stage 1 execution crashed: {e}")

            # Auto-post Stage 1 findings to TaskBoard
            try:
                from app.agents.task_board import task_board
                for label, insight in results.items():
                    if isinstance(insight, str) and not insight.startswith("Error:"):
                        snippet = insight[:500] if len(insight) > 500 else insight
                        await task_board.post_finding(
                            source_agent=f"{label}_agent",
                            content=snippet,
                            ticker=entity_id,
                            cycle_id=cycle_id,
                            category="fact",
                            confidence=75,
                        )
            except Exception as tb_err:
                logger.warning(f"[{entity_id}] MetaOrchestrator Stage 1 TaskBoard post failed: {tb_err}")

        # Retrieve findings from the TaskBoard for Stage 2
        team_findings_str = ""
        try:
            from app.agents.task_board import task_board
            findings = await task_board.get_findings(ticker=entity_id, cycle_id=cycle_id)
            if findings:
                team_findings_str = "\n".join(
                    f"- [{f.get('source_agent', '?')}]: {f.get('content', '')}"
                    for f in findings
                )
        except Exception as tb_err:
            logger.debug(f"[{entity_id}] MetaOrchestrator failed to read Stage 1 findings: {tb_err}")

        # ── STAGE 2: Deep Research and Macro Risk (Helen / Risk) ──
        stage2_tasks = []
        stage2_labels = []

        # Rule 1.5: Deep Dive for high redundancy
        if is_highly_redundant:
            stage2_tasks.append(analyze_deep_research(entity_id, packet, cycle_id, bot_id, research_focus, team_findings_str))
            stage2_labels.append("deep_research")

        # Rule 1: If macro indicator is somewhat intact, check macro risk
        if "regime" not in packet.missing_fields:
            stage2_tasks.append(analyze_macro_risk(entity_id, packet, cycle_id, bot_id, research_focus, team_findings_str))
            stage2_labels.append("macro_risk")

        if stage2_tasks:
            logger.info(f"[{entity_id}] MetaOrchestrator Stage 2: Running {stage2_labels}")
            try:
                wrapped_tasks = [
                    asyncio.wait_for(task, timeout=PER_AGENT_TIMEOUT)
                    for task in stage2_tasks
                ]
                outputs = await concurrency_controller.gather(wrapped_tasks, label="meta_orchestrator_stage2", return_exceptions=True)
                for label, out in zip(stage2_labels, outputs):
                    if isinstance(out, asyncio.TimeoutError):
                        logger.warning(f"[{entity_id}] MetaOrchestrator: {label} TIMED OUT")
                        results[label] = f"Error: Agent timed out"
                    elif isinstance(out, Exception):
                        logger.error(f"[{entity_id}] MetaOrchestrator task {label} failed: {out}")
                        results[label] = f"Error: {out}"
                    else:
                        results[label] = out[0]
                        total_tokens += out[1]
            except Exception as e:
                logger.error(f"[{entity_id}] MetaOrchestrator Stage 2 execution crashed: {e}")

            # Auto-post Stage 2 findings to TaskBoard
            try:
                from app.agents.task_board import task_board
                for label, insight in results.items():
                    if label in stage2_labels and isinstance(insight, str) and not insight.startswith("Error:"):
                        snippet = insight[:500] if len(insight) > 500 else insight
                        await task_board.post_finding(
                            source_agent=f"{label}_agent",
                            content=snippet,
                            ticker=entity_id,
                            cycle_id=cycle_id,
                            category="fact",
                            confidence=75,
                        )
            except Exception as tb_err:
                logger.warning(f"[{entity_id}] MetaOrchestrator Stage 2 TaskBoard post failed: {tb_err}")

        if not results:
            logger.info(f"[{entity_id}] MetaOrchestrator: Evidence too sparse for specialized agents.")
            return {}, 0

        return results, total_tokens


