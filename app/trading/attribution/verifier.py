"""Lineage and Invariant Verifier for the Control Plane.

Verifies end-to-end structural integrity across the attribution chain:
DecisionArtifact -> PolicyDecision -> ExecutionIntent -> OrderAttempt
-> trade_fills -> ExecutionReconciliation -> AttributionReport.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db import mongo_query, mongo_store
from app.trading.attribution.repository import (
    COLL_ATTRIBUTION_REPORTS,
    COLL_DECISION_ARTIFACTS,
    COLL_EXECUTION_INTENTS,
    COLL_EXECUTION_RECONCILIATIONS,
    COLL_ORDER_ATTEMPTS,
    COLL_POLICY_DECISIONS,
    get_decision_artifact,
    get_execution_intent,
    get_policy_decision,
)

logger = logging.getLogger(__name__)


class LineageVerificationResult:
    def __init__(self, target_id: str):
        self.target_id = target_id
        self.valid = True
        self.anomalies: list[str] = []
        self.chain: dict[str, Any] = {}

    def add_anomaly(self, message: str) -> None:
        self.valid = False
        self.anomalies.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "valid": self.valid,
            "anomalies": self.anomalies,
            "chain": self.chain,
        }


def verify_intent_lineage(execution_intent_id: str) -> LineageVerificationResult:
    """Verifies that an ExecutionIntent has valid, unbroken upstream parents."""
    result = LineageVerificationResult(execution_intent_id)
    intent = get_execution_intent(execution_intent_id)
    if not intent:
        result.add_anomaly(f"ExecutionIntent {execution_intent_id} not found")
        return result
    result.chain["intent"] = intent.model_dump(mode="python")

    # Upstream: PolicyDecision
    policy = get_policy_decision(intent.policy_decision_id)
    if not policy:
        result.add_anomaly(
            f"Orphan intent: PolicyDecision {intent.policy_decision_id} not found"
        )
    else:
        result.chain["policy"] = policy.model_dump(mode="python")
        if policy.decision_id != intent.decision_id:
            result.add_anomaly(
                f"Lineage mismatch: Policy decision_id {policy.decision_id} != Intent decision_id {intent.decision_id}"
            )

        # Upstream: DecisionArtifact
        artifact = get_decision_artifact(policy.decision_id)
        if not artifact:
            result.add_anomaly(
                f"Orphan policy: DecisionArtifact {policy.decision_id} not found"
            )
        else:
            result.chain["artifact"] = artifact.model_dump(mode="python")

    # Downstream: Order attempts and fills
    attempts = mongo_store.find_docs(
        COLL_ORDER_ATTEMPTS, {"execution_intent_id": execution_intent_id}
    )
    result.chain["order_attempts"] = attempts

    fills = mongo_store.find_docs(
        "trade_fills", {"execution_intent_id": execution_intent_id}
    )
    result.chain["trade_fills"] = fills

    reconciliations = mongo_store.find_docs(
        COLL_EXECUTION_RECONCILIATIONS, {"execution_intent_id": execution_intent_id}
    )
    result.chain["reconciliations"] = reconciliations

    return result


def verify_fill_lineage(fill_id: str) -> LineageVerificationResult:
    """Verifies that a broker fill traces cleanly back to an approved Intent and Decision."""
    result = LineageVerificationResult(fill_id)
    fill_rows = mongo_store.find_docs("trade_fills", {"fill_id": fill_id}, limit=1)
    if not fill_rows:
        result.add_anomaly(f"trade_fill {fill_id} not found")
        return result
    fill = fill_rows[0]
    result.chain["fill"] = fill

    intent_id = fill.get("execution_intent_id")
    if not intent_id:
        result.add_anomaly(f"trade_fill {fill_id} has no execution_intent_id (unattributed fill)")
        return result

    intent_res = verify_intent_lineage(intent_id)
    result.valid = intent_res.valid
    result.anomalies.extend(intent_res.anomalies)
    result.chain.update(intent_res.chain)
    return result
