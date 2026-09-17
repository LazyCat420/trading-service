"""Standardized Failure Attribution Engine.

Replaces generic 'trade lost' diagnoses with structured, actionable ownership
across the 10-level attribution taxonomy.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from app.trading.attribution.models import (
    AttributionClass,
    AttributionReport,
    PolicyDisposition,
    ReconciliationVerdict,
)

logger = logging.getLogger(__name__)


class AttributionClassifier:
    """Classifies lifecycle outcomes and terminal events into root cause attributions."""

    @staticmethod
    def classify(
        lineage: dict[str, Any],
        disposition: Optional[str] = None,
        reconciliation_verdict: Optional[str] = None,
        decision_alpha: Optional[float] = None,
        net_alpha: Optional[float] = None,
        execution_drag: Optional[float] = None,
        data_error: Optional[str] = None,
        infra_error: Optional[str] = None,
        ledger_error: Optional[str] = None,
        outcome_status: Optional[str] = None,
    ) -> AttributionReport:
        report_id = f"att-rep-{uuid.uuid4().hex[:12]}"

        # 1. Check Infrastructure Failures
        if infra_error:
            return AttributionReport(
                attribution_id=report_id,
                lineage=lineage,
                classification=AttributionClass.INFRASTRUCTURE_FAILURE,
                primary_reason_code=infra_error,
                owner_subsystem="SYSTEM_INFRA",
                remediation_route="INFRA_HEALTH_INVESTIGATION",
            )

        # 2. Check Market Data Failures
        if data_error:
            return AttributionReport(
                attribution_id=report_id,
                lineage=lineage,
                classification=AttributionClass.MARKET_DATA_FAILURE,
                primary_reason_code=data_error,
                owner_subsystem="DATA_FEED",
                remediation_route="DATA_COLLECTOR_HEALTH_CHECK",
            )

        # 3. Check Ledger / Accounting Failures
        if ledger_error:
            return AttributionReport(
                attribution_id=report_id,
                lineage=lineage,
                classification=AttributionClass.LEDGER_FAILURE,
                primary_reason_code=ledger_error,
                owner_subsystem="LEDGER_ENGINE",
                remediation_route="LEDGER_RECONCILIATION_RUN",
            )

        # 4. Check Outcome Label / Evidence Failures
        if outcome_status in ("CONTAMINATED", "CANCELED"):
            return AttributionReport(
                attribution_id=report_id,
                lineage=lineage,
                classification=AttributionClass.OUTCOME_LABEL_FAILURE,
                primary_reason_code=f"OUTCOME_{outcome_status}",
                owner_subsystem="OUTCOME_RESOLVER",
                remediation_route="OUTCOME_EVIDENCE_AUDIT",
            )

        # 5. Check Policy Interventions
        if disposition in (
            PolicyDisposition.BLOCK.value,
            PolicyDisposition.CONVERT_TO_WATCH.value,
            PolicyDisposition.QUEUE_RESEARCH.value,
            PolicyDisposition.REJECT.value,
        ):
            return AttributionReport(
                attribution_id=report_id,
                lineage=lineage,
                classification=AttributionClass.POLICY_INTERVENTION,
                primary_reason_code=f"POLICY_{disposition}",
                owner_subsystem="POLICY_GATE",
                remediation_route="POLICY_THRESHOLD_REVIEW",
            )

        # 6. Check Execution Failures
        if reconciliation_verdict in (
            ReconciliationVerdict.EXECUTION_REJECTED.value,
            ReconciliationVerdict.EXECUTION_SLIPPAGE_BREACH.value,
            ReconciliationVerdict.EXECUTION_COST_BREACH.value,
            ReconciliationVerdict.EXECUTION_LATE.value,
            ReconciliationVerdict.EXECUTION_DUPLICATE.value,
            ReconciliationVerdict.EXECUTION_ORPHANED.value,
        ):
            return AttributionReport(
                attribution_id=report_id,
                lineage=lineage,
                classification=AttributionClass.EXECUTION_FAILURE,
                primary_reason_code=reconciliation_verdict,
                owner_subsystem="PAPER_TRADER",
                remediation_route="EXECUTION_SLIPPAGE_CALIBRATION",
            )

        # 7. Check Outcome Performance
        if decision_alpha is None or net_alpha is None:
            return AttributionReport(
                attribution_id=report_id,
                lineage=lineage,
                classification=AttributionClass.UNRESOLVED,
                primary_reason_code="OUTCOME_PENDING_MATURITY",
                owner_subsystem="OUTCOME_RESOLVER",
                remediation_route="AWAIT_HORIZON_MATURITY",
            )

        # Adverse execution drag turning positive thesis into negative realized alpha
        if decision_alpha > 0 and net_alpha <= 0:
            return AttributionReport(
                attribution_id=report_id,
                lineage=lineage,
                classification=AttributionClass.EXECUTION_FAILURE,
                primary_reason_code="ADVERSE_EXECUTION_DRAG",
                contributing_factors=[{"execution_drag": execution_drag}],
                owner_subsystem="PAPER_TRADER",
                remediation_route="SLIPPAGE_AND_SPREAD_AUDIT",
            )

        # Negative decision alpha (underlying thesis was wrong)
        if decision_alpha <= 0:
            return AttributionReport(
                attribution_id=report_id,
                lineage=lineage,
                classification=AttributionClass.DECISION_FAILURE,
                primary_reason_code="NEGATIVE_DECISION_ALPHA",
                contributing_factors=[{"decision_alpha": decision_alpha}],
                owner_subsystem="LLM_AGENT",
                remediation_route="PROMPT_AND_SKILL_OPTIMIZATION",
            )

        # Successful trade
        return AttributionReport(
            attribution_id=report_id,
            lineage=lineage,
            classification=AttributionClass.NO_FAILURE,
            primary_reason_code="POSITIVE_NET_ALPHA",
            contributing_factors=[{"net_alpha": net_alpha, "decision_alpha": decision_alpha}],
            owner_subsystem="LLM_AGENT",
            remediation_route="NONE",
        )
