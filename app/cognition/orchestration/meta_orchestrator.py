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
    analyze_quantitative_critique,
)

logger = logging.getLogger(__name__)


from app.services.adaptive_concurrency import concurrency_controller

class MetaOrchestrator:
    """
    Deterministically routes to specialized agents depending on the EvidencePacket
    and Data Sufficiency.

    Stage execution order (per blueprint):
      Stage 0: Janitor (Ray) — data integrity check
      Stage 1: Fundamental (Priya) + Sentiment (Vance) — qualitative analysis
      Stage 2: Quant (Aris) — mathematical critique of Stage 1 findings
      Stage 3: Macro Risk (Helen) + Deep Research — risk assessment

    Each stage posts to TaskBoard. Later stages read earlier findings.
    Between stages, DELEGATION follow-ups are processed.
    """

    @staticmethod
    async def _run_stage(
        entity_id: str,
        tasks: list,
        labels: list[str],
        results: dict,
        stage_name: str,
        per_agent_timeout: float,
    ) -> int:
        """Run a stage of agents concurrently, collect results. Returns tokens used."""
        tokens = 0
        if not tasks:
            return 0

        logger.info(f"[{entity_id}] MetaOrchestrator {stage_name}: Running {labels}")
        try:
            wrapped = [
                asyncio.wait_for(task, timeout=per_agent_timeout)
                for task in tasks
            ]
            outputs = await concurrency_controller.gather(
                wrapped, label=f"meta_orchestrator_{stage_name}", return_exceptions=True,
            )
            for label, out in zip(labels, outputs):
                if isinstance(out, asyncio.TimeoutError):
                    logger.warning(f"[{entity_id}] MetaOrchestrator: {label} TIMED OUT")
                    results[label] = f"Error: Agent timed out"
                elif isinstance(out, Exception):
                    logger.error(f"[{entity_id}] MetaOrchestrator task {label} failed: {out}")
                    results[label] = f"Error: {out}"
                else:
                    results[label] = out[0]
                    tokens += out[1]
        except Exception as e:
            logger.error(f"[{entity_id}] MetaOrchestrator {stage_name} execution crashed: {e}")

        return tokens

    @staticmethod
    async def _post_findings_to_taskboard(
        entity_id: str,
        results: dict,
        stage_labels: list[str],
        cycle_id: str,
    ) -> None:
        """Post findings from a stage's labels to the TaskBoard."""
        try:
            from app.agents.task_board import task_board
            for label in stage_labels:
                insight = results.get(label, "")
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
            logger.warning(f"[{entity_id}] MetaOrchestrator TaskBoard post failed: {tb_err}")

    @staticmethod
    async def _get_team_findings(entity_id: str, cycle_id: str) -> str:
        """Retrieve current TaskBoard findings as a formatted string for downstream agents."""
        try:
            from app.agents.task_board import task_board
            findings = await task_board.get_findings(ticker=entity_id, cycle_id=cycle_id)
            if findings:
                return "\n".join(
                    f"- [{f.get('source_agent', '?')}]: {f.get('content', '')}"
                    for f in findings
                )
        except Exception as tb_err:
            logger.debug(f"[{entity_id}] MetaOrchestrator failed to read findings: {tb_err}")
        return ""

    @staticmethod
    async def _process_delegations(entity_id: str, cycle_id: str, bot_id: str) -> None:
        """Process any DELEGATION follow-ups from recent TaskBoard findings."""
        try:
            from app.agents.delegation_handler import process_delegations_from_findings
            count = await process_delegations_from_findings(entity_id, cycle_id, bot_id)
            if count > 0:
                logger.info(
                    "[%s] MetaOrchestrator: Processed %d delegation follow-up(s)",
                    entity_id, count,
                )
        except Exception as e:
            logger.debug("[%s] MetaOrchestrator delegation scan failed: %s", entity_id, e)

    @staticmethod
    async def orchestrate(
        entity_id: str,
        packet: EvidencePacket,
        sufficiency: SufficiencyResult,
        cycle_id: str,
        bot_id: str,
        is_highly_redundant: bool = False,
        research_focus: str = "",
        trigger_type: str = "manual",
    ) -> tuple[Dict[str, str], int]:
        """
        4-stage staggered pipeline. Each stage reads earlier findings via TaskBoard.
        Returns a dict of agent_name -> insight, and total tokens used.
        """
        results = {}
        total_tokens = 0
        PER_AGENT_TIMEOUT = 600.0

        # ════════════════════════════════════════════════════════════
        #  STAGE 0: JANITOR (Ray) — Data integrity check
        # ════════════════════════════════════════════════════════════
        try:
            from app.agents.data_integrity_agent import run_data_integrity_check

            logger.info(f"[{entity_id}] MetaOrchestrator Stage 0: Janitor (Ray) data integrity check")
            janitor_result, janitor_tokens = await asyncio.wait_for(
                run_data_integrity_check(entity_id, packet, cycle_id, bot_id),
                timeout=PER_AGENT_TIMEOUT,
            )
            results["data_integrity"] = janitor_result
            total_tokens += janitor_tokens

            # Post janitor findings to TaskBoard
            await MetaOrchestrator._post_findings_to_taskboard(
                entity_id, results, ["data_integrity"], cycle_id,
            )
            logger.info(f"[{entity_id}] MetaOrchestrator Stage 0: Janitor complete ({janitor_tokens} tokens)")

        except asyncio.TimeoutError:
            logger.warning(f"[{entity_id}] MetaOrchestrator: Janitor TIMED OUT")
            results["data_integrity"] = "Error: Janitor timed out"
        except Exception as e:
            logger.error(f"[{entity_id}] MetaOrchestrator Stage 0 failed: {e}")
            results["data_integrity"] = f"Error: {e}"

        # Process any delegations from Stage 0
        await MetaOrchestrator._process_delegations(entity_id, cycle_id, bot_id)

        # ════════════════════════════════════════════════════════════
        #  STAGE 1: QUALITATIVE — Sentiment (Vance) + Fundamentals (Priya)
        # ════════════════════════════════════════════════════════════
        stage1_tasks = []
        stage1_labels = []

        # Sentiment (Vance) — if sentiment features are intact
        if (
            "sentiment" not in packet.missing_fields
            and "news" not in packet.missing_fields
        ):
            stage1_tasks.append(analyze_sentiment(entity_id, packet, cycle_id, bot_id, research_focus))
            stage1_labels.append("sentiment")

        # Fundamentals (Priya) — if basic financials exist
        if (
            "fundamentals" not in packet.missing_fields
            and "pe_ratio" not in packet.missing_fields
        ):
            stage1_tasks.append(analyze_fundamentals(entity_id, packet, cycle_id, bot_id, research_focus))
            stage1_labels.append("fundamentals")

        if stage1_tasks:
            stage1_tokens = await MetaOrchestrator._run_stage(
                entity_id, stage1_tasks, stage1_labels, results,
                "Stage 1 (Qualitative)", PER_AGENT_TIMEOUT,
            )
            total_tokens += stage1_tokens

            # Post Stage 1 findings to TaskBoard
            await MetaOrchestrator._post_findings_to_taskboard(
                entity_id, results, stage1_labels, cycle_id,
            )
            logger.info(
                f"[{entity_id}] MetaOrchestrator: Posted {len(stage1_labels)} Stage 1 findings to TaskBoard"
            )

        # Process delegations between Stage 1 and Stage 2
        await MetaOrchestrator._process_delegations(entity_id, cycle_id, bot_id)

        # ════════════════════════════════════════════════════════════
        #  STAGE 2: QUANTITATIVE — Dr. Aris critiques Stage 0+1 findings
        # ════════════════════════════════════════════════════════════
        if trigger_type != "smoke_test":
            # ════════════════════════════════════════════════════════════
            #  STAGE 2: QUANTITATIVE — Dr. Aris critiques Stage 0+1 findings
            # ════════════════════════════════════════════════════════════
            team_findings_str = await MetaOrchestrator._get_team_findings(entity_id, cycle_id)

            if team_findings_str:
                stage2_tasks = [
                    analyze_quantitative_critique(
                        entity_id, packet, cycle_id, bot_id, research_focus, team_findings_str,
                    ),
                ]
                stage2_labels = ["quant_critique"]

                stage2_tokens = await MetaOrchestrator._run_stage(
                    entity_id, stage2_tasks, stage2_labels, results,
                    "Stage 2 (Quantitative)", PER_AGENT_TIMEOUT,
                )
                total_tokens += stage2_tokens

                # Post Stage 2 findings to TaskBoard
                await MetaOrchestrator._post_findings_to_taskboard(
                    entity_id, results, stage2_labels, cycle_id,
                )
                logger.info(
                    f"[{entity_id}] MetaOrchestrator: Dr. Aris critique posted to TaskBoard"
                )

                # Process delegations from Aris
                await MetaOrchestrator._process_delegations(entity_id, cycle_id, bot_id)
            else:
                logger.info(f"[{entity_id}] MetaOrchestrator: Skipping Stage 2 — no prior findings to critique")

            # Refresh team findings for Stage 3 (now includes Stages 0, 1, and 2)
            team_findings_str = await MetaOrchestrator._get_team_findings(entity_id, cycle_id)

            # ════════════════════════════════════════════════════════════
            #  STAGE 3: RISK — Macro Risk (Helen) + Deep Research
            # ════════════════════════════════════════════════════════════
            stage3_tasks = []
            stage3_labels = []

            # Deep Research — for highly redundant tickers
            if is_highly_redundant:
                stage3_tasks.append(
                    analyze_deep_research(entity_id, packet, cycle_id, bot_id, research_focus, team_findings_str),
                )
                stage3_labels.append("deep_research")

            # Macro Risk (Helen) — if regime data exists
            if "regime" not in packet.missing_fields:
                stage3_tasks.append(
                    analyze_macro_risk(entity_id, packet, cycle_id, bot_id, research_focus, team_findings_str),
                )
                stage3_labels.append("macro_risk")

            if stage3_tasks:
                stage3_tokens = await MetaOrchestrator._run_stage(
                    entity_id, stage3_tasks, stage3_labels, results,
                    "Stage 3 (Risk)", PER_AGENT_TIMEOUT,
                )
                total_tokens += stage3_tokens

                # Post Stage 3 findings to TaskBoard
                await MetaOrchestrator._post_findings_to_taskboard(
                    entity_id, results, stage3_labels, cycle_id,
                )

            # Process final round of delegations
            await MetaOrchestrator._process_delegations(entity_id, cycle_id, bot_id)
        else:
            logger.info(f"[{entity_id}] Smoke test detected — skipping Stage 2 (Quantitative) and Stage 3 (Risk) specialist agents.")

        # Clean up delegation budget for this ticker
        try:
            from app.agents.delegation_handler import clear_delegation_budget
            clear_delegation_budget(entity_id, cycle_id)
        except Exception:
            pass

        if not results:
            logger.info(f"[{entity_id}] MetaOrchestrator: Evidence too sparse for specialized agents.")
            return {}, 0

        # Log total agent dispatch summary
        active_labels = [k for k, v in results.items() if isinstance(v, str) and not v.startswith("Error:")]
        logger.info(
            f"[{entity_id}] MetaOrchestrator: Dispatching {active_labels} (timeout={PER_AGENT_TIMEOUT}s each)"
        )

        return results, total_tokens


