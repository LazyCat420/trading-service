"""Durable Repository and Persistence Layer for Attribution Records.

Provides idempotent write operations, atomic CAS for execution intents,
and unique index definitions in MongoDB.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Optional

from app.db import mongo_query, mongo_store
from app.trading.attribution.models import (
    AttributionReport,
    DecisionArtifact,
    DecisionOutcomeRecord,
    ExecutionIntent,
    ExecutionReconciliation,
    IntentStatus,
    OrderAttempt,
    PolicyDecision,
)

logger = logging.getLogger(__name__)

COLL_DECISION_ARTIFACTS = "decision_artifacts"
COLL_POLICY_DECISIONS = "policy_decisions"
COLL_EXECUTION_INTENTS = "execution_intents"
COLL_ORDER_ATTEMPTS = "order_attempts"
COLL_EXECUTION_RECONCILIATIONS = "execution_reconciliations"
COLL_ATTRIBUTION_REPORTS = "attribution_reports"


def ensure_attribution_indexes() -> None:
    """Create unique and lookup indexes for canonical attribution tables."""
    try:
        db = mongo_store.get_doc_db()

        # 1. decision_artifacts
        c_da = db[COLL_DECISION_ARTIFACTS]
        c_da.create_index([("decision_id", 1)], unique=True)
        c_da.create_index([("cycle_id", 1), ("ticker", 1)])

        # 2. policy_decisions
        c_pd = db[COLL_POLICY_DECISIONS]
        c_pd.create_index([("policy_decision_id", 1)], unique=True)
        c_pd.create_index([("decision_id", 1)], unique=True)

        # 3. execution_intents
        c_ei = db[COLL_EXECUTION_INTENTS]
        c_ei.create_index([("execution_intent_id", 1)], unique=True)
        c_ei.create_index([("idempotency_key", 1)], unique=True)
        c_ei.create_index([("decision_id", 1)])

        # 4. order_attempts
        c_oa = db[COLL_ORDER_ATTEMPTS]
        c_oa.create_index([("order_attempt_id", 1)], unique=True)
        c_oa.create_index([("execution_intent_id", 1), ("attempt_number", 1)], unique=True)

        # 5. execution_reconciliations
        c_er = db[COLL_EXECUTION_RECONCILIATIONS]
        c_er.create_index([("reconciliation_id", 1)], unique=True)
        c_er.create_index([("execution_intent_id", 1)], unique=True)

        # 6. attribution_reports
        c_ar = db[COLL_ATTRIBUTION_REPORTS]
        c_ar.create_index([("attribution_id", 1)], unique=True)
        c_ar.create_index([("lineage.decision_id", 1)])
        logger.info("[AttributionRepo] Indexes ensured for canonical attribution collections.")
    except Exception as exc:
        logger.warning("[AttributionRepo] ensure_attribution_indexes non-fatal failure: %s", exc)


def save_decision_artifact(artifact: DecisionArtifact) -> DecisionArtifact:
    """Persist DecisionArtifact idempotently. Never overwrites historical proposal."""
    doc = artifact.model_dump(mode="python")
    existing = mongo_query.find_row(
        COLL_DECISION_ARTIFACTS, {"decision_id": artifact.decision_id}, ["decision_id"]
    )
    if existing:
        return artifact
    mongo_store.insert_docs(COLL_DECISION_ARTIFACTS, [doc])
    return artifact


def get_decision_artifact(decision_id: str) -> Optional[DecisionArtifact]:
    docs = mongo_store.find_docs(COLL_DECISION_ARTIFACTS, {"decision_id": decision_id}, limit=1)
    if not docs:
        return None
    return DecisionArtifact.model_validate(docs[0])


def get_decision_artifact_by_cycle_ticker(cycle_id: str, ticker: str) -> Optional[DecisionArtifact]:
    docs = mongo_store.find_docs(
        COLL_DECISION_ARTIFACTS, {"cycle_id": cycle_id, "ticker": ticker}, limit=1
    )
    if not docs:
        return None
    return DecisionArtifact.model_validate(docs[0])


def save_policy_decision(policy: PolicyDecision) -> PolicyDecision:
    """Persist PolicyDecision idempotently."""
    doc = policy.model_dump(mode="python")
    existing = mongo_query.find_row(
        COLL_POLICY_DECISIONS,
        {"policy_decision_id": policy.policy_decision_id},
        ["policy_decision_id"],
    )
    if existing:
        return policy
    mongo_store.insert_docs(COLL_POLICY_DECISIONS, [doc])
    return policy


def get_policy_decision(policy_decision_id: str) -> Optional[PolicyDecision]:
    docs = mongo_store.find_docs(
        COLL_POLICY_DECISIONS, {"policy_decision_id": policy_decision_id}, limit=1
    )
    if not docs:
        return None
    return PolicyDecision.model_validate(docs[0])


def get_policy_decision_by_decision(decision_id: str) -> Optional[PolicyDecision]:
    docs = mongo_store.find_docs(
        COLL_POLICY_DECISIONS, {"decision_id": decision_id}, limit=1
    )
    if not docs:
        return None
    return PolicyDecision.model_validate(docs[0])


def save_execution_intent(intent: ExecutionIntent) -> ExecutionIntent:
    """Persist ExecutionIntent idempotently keyed on idempotency_key and execution_intent_id."""
    doc = intent.model_dump(mode="python")
    existing = mongo_query.find_row(
        COLL_EXECUTION_INTENTS,
        {"idempotency_key": intent.idempotency_key},
        ["execution_intent_id"],
    )
    if existing:
        stored_id = str(existing[0])
        existing_doc = get_execution_intent(stored_id)
        if existing_doc:
            return existing_doc
    mongo_store.insert_docs(COLL_EXECUTION_INTENTS, [doc])
    return intent


def get_execution_intent(execution_intent_id: str) -> Optional[ExecutionIntent]:
    docs = mongo_store.find_docs(
        COLL_EXECUTION_INTENTS, {"execution_intent_id": execution_intent_id}, limit=1
    )
    if not docs:
        return None
    return ExecutionIntent.model_validate(docs[0])


def get_execution_intent_by_decision(decision_id: str) -> Optional[ExecutionIntent]:
    docs = mongo_store.find_docs(
        COLL_EXECUTION_INTENTS, {"decision_id": decision_id}, limit=1
    )
    if not docs:
        return None
    return ExecutionIntent.model_validate(docs[0])


def consume_execution_intent(
    execution_intent_id: str,
    consumed_at: Optional[datetime.datetime] = None,
    session: Any = None,
) -> bool:
    """Atomic compare-and-set: transitions an ExecutionIntent from CREATED to CONSUMED.

    Returns True if consumption succeeded, False if already consumed, expired, or absent.
    """
    now = consumed_at or datetime.datetime.now(datetime.timezone.utc)
    from app.db.mongo import get_collection

    col = get_collection(COLL_EXECUTION_INTENTS)
    result = col.update_one(
        {
            "execution_intent_id": execution_intent_id,
            "status": IntentStatus.CREATED.value,
            "expires_at": {"$gt": now},
        },
        {
            "$set": {
                "status": IntentStatus.CONSUMED.value,
                "consumed_at": now,
            }
        },
        session=session,
    )
    return result.modified_count == 1


def save_order_attempt(attempt: OrderAttempt) -> OrderAttempt:
    """Persist OrderAttempt idempotently."""
    doc = attempt.model_dump(mode="python")
    existing = mongo_query.find_row(
        COLL_ORDER_ATTEMPTS,
        {"order_attempt_id": attempt.order_attempt_id},
        ["order_attempt_id"],
    )
    if existing:
        return attempt
    mongo_store.insert_docs(COLL_ORDER_ATTEMPTS, [doc])
    return attempt


def save_execution_reconciliation(rec: ExecutionReconciliation) -> ExecutionReconciliation:
    """Persist ExecutionReconciliation idempotently."""
    doc = rec.model_dump(mode="python")
    existing = mongo_query.find_row(
        COLL_EXECUTION_RECONCILIATIONS,
        {"reconciliation_id": rec.reconciliation_id},
        ["reconciliation_id"],
    )
    if existing:
        return rec
    mongo_store.insert_docs(COLL_EXECUTION_RECONCILIATIONS, [doc])
    return rec


def save_attribution_report(report: AttributionReport) -> AttributionReport:
    """Persist AttributionReport idempotently."""
    doc = report.model_dump(mode="python")
    existing = mongo_query.find_row(
        COLL_ATTRIBUTION_REPORTS,
        {"attribution_id": report.attribution_id},
        ["attribution_id"],
    )
    if existing:
        return report
    mongo_store.insert_docs(COLL_ATTRIBUTION_REPORTS, [doc])
    return report


def get_attribution_report(attribution_id: str) -> Optional[AttributionReport]:
    docs = mongo_store.find_docs(
        COLL_ATTRIBUTION_REPORTS, {"attribution_id": attribution_id}, limit=1
    )
    if not docs:
        return None
    return AttributionReport.model_validate(docs[0])


def get_attribution_report_by_decision(decision_id: str) -> Optional[AttributionReport]:
    docs = mongo_store.find_docs(
        COLL_ATTRIBUTION_REPORTS, {"lineage.decision_id": decision_id}, limit=1
    )
    if not docs:
        return None
    return AttributionReport.model_validate(docs[0])
