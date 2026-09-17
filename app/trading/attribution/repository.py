"""Durable Repository and Persistence Layer for Attribution Records.

Provides idempotent write operations, atomic CAS for execution intents,
and unique index definitions in MongoDB.
"""

from __future__ import annotations

import datetime
import logging
import uuid
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
    PolicyDisposition,
    ReservationStatus,
    RiskReservation,
)

logger = logging.getLogger(__name__)

COLL_DECISION_ARTIFACTS = "decision_artifacts"
COLL_POLICY_DECISIONS = "policy_decisions"
COLL_EXECUTION_INTENTS = "execution_intents"
COLL_ORDER_ATTEMPTS = "order_attempts"
COLL_EXECUTION_RECONCILIATIONS = "execution_reconciliations"
COLL_ATTRIBUTION_REPORTS = "attribution_reports"
COLL_DECISION_OUTCOMES = "decision_outcomes"
COLL_EXECUTION_SLOTS = "execution_slots"
COLL_EXECUTION_OUTBOX = "execution_outbox"
COLL_POSITION_LOTS = "position_lots"
COLL_LOT_CLOSURES = "lot_closures"
COLL_POLICY_SNAPSHOTS = "policy_snapshots"
COLL_RISK_RESERVATIONS = "risk_reservations"
COLL_SHADOW_EXECUTIONS = "shadow_executions"
COLL_DECISION_QUARANTINE = "decision_quarantine"
COLL_EVALUATION_CHECKPOINTS = "evaluation_checkpoints"


def ensure_attribution_indexes() -> None:
    """Create unique and lookup indexes for canonical attribution tables."""
    try:
        db = mongo_store.get_doc_db()

        # 1. decision_artifacts
        c_da = db[COLL_DECISION_ARTIFACTS]
        c_da.create_index([("decision_id", 1)], unique=True)
        c_da.create_index([("cycle_id", 1), ("ticker", 1)])
        c_da.create_index([
            ("is_quarantined", 1),
            ("outcome_status", 1),
            ("maturity_date", 1),
            ("retry_after", 1),
        ])

        # 2. policy_decisions
        c_pd = db[COLL_POLICY_DECISIONS]
        c_pd.create_index([("policy_decision_id", 1)], unique=True)
        c_pd.create_index([("decision_id", 1)], unique=True)

        # 3. execution_intents
        c_ei = db[COLL_EXECUTION_INTENTS]
        c_ei.create_index([("execution_intent_id", 1)], unique=True)
        c_ei.create_index([("idempotency_key", 1)], unique=True)
        c_ei.create_index([("decision_id", 1)])
        c_ei.create_index([("slot_key", 1)])

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

        # 7. execution_slots
        c_es = db[COLL_EXECUTION_SLOTS]
        c_es.create_index([("slot_key", 1)], unique=True)
        c_es.create_index([("decision_id", 1)])

        # 8. execution_outbox
        c_eo = db[COLL_EXECUTION_OUTBOX]
        c_eo.create_index([("event_id", 1)], unique=True)
        c_eo.create_index([("status", 1), ("created_at", 1)])

        # 9. position_lots & lot_closures
        c_pl = db[COLL_POSITION_LOTS]
        c_pl.create_index([("lot_id", 1)], unique=True)
        c_pl.create_index([("bot_id", 1), ("ticker", 1), ("status", 1)])

        c_lc = db[COLL_LOT_CLOSURES]
        c_lc.create_index([("closure_id", 1)], unique=True)
        c_lc.create_index([("lot_id", 1)])

        # 10. risk_reservations
        c_rr = db[COLL_RISK_RESERVATIONS]
        c_rr.create_index([("reservation_id", 1)], unique=True)
        c_rr.create_index([("execution_intent_id", 1)])
        c_rr.create_index([("bot_id", 1), ("status", 1)])
        c_rr.create_index([("slot_key", 1)])

        # 11. shadow_executions
        c_se = db[COLL_SHADOW_EXECUTIONS]
        c_se.create_index([("execution_intent_id", 1)], unique=True)
        c_se.create_index([("order_id", 1)], unique=True)
        c_se.create_index([("bot_id", 1), ("ticker", 1)])

        # 12. decision_quarantine
        c_dq = db[COLL_DECISION_QUARANTINE]
        c_dq.create_index([("decision_id", 1)])
        c_dq.create_index([("quarantined_at", -1)])

        # 13. evaluation_checkpoints
        c_ec = db[COLL_EVALUATION_CHECKPOINTS]
        c_ec.create_index([("worker", 1)], unique=True)

        logger.info("[AttributionRepo] Indexes ensured for canonical attribution and control-plane collections.")
    except Exception as exc:
        logger.warning("[AttributionRepo] ensure_attribution_indexes non-fatal failure: %s", exc)


def save_evaluation_checkpoint(worker_name: str, records_processed: int, last_evaluated_at: datetime.datetime) -> None:
    """Records an evaluation checkpoint for workers to provide operational visibility and restart safety."""
    db = mongo_store.get_doc_db()
    db[COLL_EVALUATION_CHECKPOINTS].update_one(
        {"worker": worker_name},
        {"$set": {
            "worker": worker_name,
            "last_evaluated_at": last_evaluated_at,
            "records_processed": records_processed,
            "updated_at": datetime.datetime.now(datetime.timezone.utc),
        }},
        upsert=True,
    )


def get_evaluation_checkpoint(worker_name: str) -> Optional[dict[str, Any]]:
    """Retrieves the last saved evaluation checkpoint for a worker."""
    db = mongo_store.get_doc_db()
    return db[COLL_EVALUATION_CHECKPOINTS].find_one({"worker": worker_name})


def save_decision_artifact(artifact: DecisionArtifact) -> DecisionArtifact:
    """Persist DecisionArtifact idempotently. Never overwrites historical proposal."""
    doc = artifact.model_dump(mode="python")
    existing = mongo_query.find_row(
        COLL_DECISION_ARTIFACTS, {"decision_id": artifact.decision_id}, ["decision_id"]
    )
    if existing:
        return artifact
    mongo_store.insert_docs(COLL_DECISION_ARTIFACTS, [doc])
    try:
        from app.telemetry.trading_adapter import TradingLineageTracker
        TradingLineageTracker.record_decision(
            cycle_id=artifact.cycle_id,
            ticker=artifact.ticker,
            decision_id=artifact.decision_id,
            action=artifact.action,
            confidence=artifact.confidence or 0,
            attributes={"strategy": getattr(artifact, "strategy_name", "")},
        )
    except Exception as e:
        logger.debug("[telemetry] record_decision failed: %s", e)
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
    try:
        from app.telemetry.trading_adapter import TradingLineageTracker
        approved = policy.disposition in (PolicyDisposition.APPROVE, PolicyDisposition.APPROVE_WITH_CAP)
        TradingLineageTracker.record_policy_eval(
            cycle_id=policy.cycle_id,
            ticker=policy.ticker,
            policy_decision_id=policy.policy_decision_id,
            decision_id=policy.decision_id,
            verdict=policy.disposition.value,
            approved=approved,
            attributes={"rationale": getattr(policy, "rationale", "")},
        )
    except Exception as e:
        logger.debug("[telemetry] record_policy_eval failed: %s", e)
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
    db = mongo_store.get_doc_db()
    col = db[COLL_EXECUTION_INTENTS]
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


def save_order_attempt(attempt: OrderAttempt, session: Optional[Any] = None) -> OrderAttempt:
    """Persist OrderAttempt idempotently."""
    doc = attempt.model_dump(mode="python")
    existing = mongo_query.find_row(
        COLL_ORDER_ATTEMPTS,
        {"order_attempt_id": attempt.order_attempt_id},
        ["order_attempt_id"],
        session=session,
    )
    if existing:
        return attempt
    mongo_store.insert_docs(COLL_ORDER_ATTEMPTS, [doc], session=session)
    return attempt


def save_execution_reconciliation(
    rec: ExecutionReconciliation,
    session: Any = None,
) -> ExecutionReconciliation:
    """Persist ExecutionReconciliation idempotently."""
    doc = rec.model_dump(mode="python")
    existing = mongo_query.find_row(
        COLL_EXECUTION_RECONCILIATIONS,
        {"reconciliation_id": rec.reconciliation_id},
        ["reconciliation_id"],
        session=session,
    )
    if existing:
        return rec
    mongo_store.insert_docs(COLL_EXECUTION_RECONCILIATIONS, [doc], session=session)
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


class AdmissionError(Exception):
    """Base exception for intent admission errors."""
    def __init__(self, message: str, reason_code: str):
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


class SlotConflictError(AdmissionError):
    pass


class IntentSupersessionError(AdmissionError):
    pass


class InsufficientCashReservationError(AdmissionError):
    pass


class DegradedSnapshotAdmissionError(AdmissionError):
    pass


class InvalidAccountAdmissionError(AdmissionError):
    pass


def claim_execution_slot(
    slot_key: str,
    decision_id: str,
    intent_id: str,
    expires_at: datetime.datetime,
    owner_token: Optional[str] = None,
    session: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """Claims an execution slot atomically with CAS versioning and owner tokens.

    Returns (True, doc) if successfully claimed or matches existing decision_id.
    Returns (False, existing_doc) if slot is currently occupied by a different active decision.
    """
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    col = db[COLL_EXECUTION_SLOTS]
    token = owner_token or f"tok-{uuid.uuid4().hex[:12]}"

    doc = col.find_one({"slot_key": slot_key}, session=session)
    if not doc:
        new_slot = {
            "slot_key": slot_key,
            "decision_id": decision_id,
            "intent_id": intent_id,
            "owner_token": token,
            "version": 1,
            "claimed_at": now,
            "expires_at": expires_at,
            "status": "ACTIVE",
        }
        try:
            col.insert_one(new_slot, session=session)
            return True, new_slot
        except Exception:
            # Concurrent race caught by unique index
            doc = col.find_one({"slot_key": slot_key}, session=session)

    if doc:
        # If same decision, return success (idempotent retry)
        if doc.get("decision_id") == decision_id:
            return True, doc
        # If existing slot expired or superseded or released, allow claim via CAS
        existing_expires = doc.get("expires_at")
        curr_ver = doc.get("version", 1)
        if (existing_expires and existing_expires < now) or doc.get("status") in (
            "SUPERSEDED",
            "EXPIRED",
            "RELEASED",
            "CONSUMED",
        ):
            res = col.update_one(
                {"slot_key": slot_key, "version": curr_ver},
                {
                    "$set": {
                        "decision_id": decision_id,
                        "intent_id": intent_id,
                        "owner_token": token,
                        "claimed_at": now,
                        "expires_at": expires_at,
                        "status": "ACTIVE",
                    },
                    "$inc": {"version": 1},
                },
                session=session,
            )
            modified = getattr(res, "modified_count", 1) if res is not None else 1
            if modified == 1:
                updated_doc = col.find_one({"slot_key": slot_key}, session=session)
                return True, updated_doc or doc
            # CAS race lost
            curr = col.find_one({"slot_key": slot_key}, session=session)
            return False, curr or doc
        # Otherwise slot is active and held by a different decision
        return False, doc

    return True, {}


def release_execution_slot(
    slot_key: str,
    owner_token: Optional[str] = None,
    session: Any = None,
) -> bool:
    """Releases an active execution slot atomically if owner_token matches (or if token is None)."""
    db = mongo_store.get_doc_db()
    col = db[COLL_EXECUTION_SLOTS]
    query: dict[str, Any] = {"slot_key": slot_key, "status": "ACTIVE"}
    if owner_token:
        query["owner_token"] = owner_token
    now = datetime.datetime.now(datetime.timezone.utc)
    res = col.update_one(
        query,
        {"$set": {"status": "RELEASED", "released_at": now}},
        session=session,
    )
    return res.modified_count == 1


def supersede_execution_intent(
    intent_id: str,
    superseded_by_decision_id: str,
    session: Any = None,
) -> bool:
    """Atomically transitions an intent from CREATED to SUPERSEDED."""
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    col = db[COLL_EXECUTION_INTENTS]
    res = col.update_one(
        {"execution_intent_id": intent_id, "status": IntentStatus.CREATED.value},
        {
            "$set": {
                "status": IntentStatus.SUPERSEDED.value,
                "superseded_by": superseded_by_decision_id,
                "superseded_at": now,
            }
        },
        session=session,
    )
    return res.modified_count > 0


def get_active_reserved_notional(bot_id: str, session: Any = None) -> float:
    """Sums reserved_notional for all ACTIVE and unexpired risk reservations for bot_id."""
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    pipeline = [
        {
            "$match": {
                "bot_id": bot_id,
                "status": ReservationStatus.ACTIVE.value,
                "expires_at": {"$gt": now},
            }
        },
        {
            "$group": {
                "_id": "$bot_id",
                "total": {"$sum": "$reserved_notional"},
            }
        },
    ]
    res = list(db[COLL_RISK_RESERVATIONS].aggregate(pipeline, session=session))
    if res and res[0].get("total") is not None:
        return float(res[0]["total"])
    return 0.0


def release_risk_reservations_for_intent(
    execution_intent_id: str,
    session: Any = None,
) -> int:
    """Transitions any ACTIVE risk reservations for an intent to RELEASED."""
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    res = db[COLL_RISK_RESERVATIONS].update_many(
        {"execution_intent_id": execution_intent_id, "status": ReservationStatus.ACTIVE.value},
        {"$set": {"status": ReservationStatus.RELEASED.value, "released_at": now}},
        session=session,
    )
    return res.modified_count


def consume_risk_reservations_for_intent(
    execution_intent_id: str,
    session: Any = None,
) -> int:
    """Transitions any ACTIVE risk reservations for an intent to CONSUMED when executed."""
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    res = db[COLL_RISK_RESERVATIONS].update_many(
        {"execution_intent_id": execution_intent_id, "status": ReservationStatus.ACTIVE.value},
        {"$set": {"status": ReservationStatus.CONSUMED.value, "consumed_at": now}},
        session=session,
    )
    return res.modified_count


def admit_execution_intent(
    intent: ExecutionIntent,
    slot_key: str,
    required_notional: float,
    policy_decision: Optional[PolicyDecision] = None,
    policy_snapshot: Optional[Any] = None,
    allow_supersede: bool = True,
    owner_token: Optional[str] = None,
    session: Any = None,
) -> dict[str, Any]:
    """Atomic multi-document transaction for intent admission, slot reservation, and cash locking.

    Invariants enforced:
    1. Validates policy decision approval and snapshot integrity (blocks BUY on degraded snapshots).
    2. Enforces request idempotency (returns existing intent on replay).
    3. Claims execution slot with compare-and-set semantics and owner token.
    4. Supersedes prior unconsumed intent on replacement; fails if prior intent already CONSUMED.
    5. Releases prior intent's risk reservations.
    6. Verifies available purchasing power (cash minus active reservations >= required notional for BUY).
    7. Atomically persists new RiskReservation and ExecutionIntent.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    bot_id = intent.bot_id
    if not bot_id or bot_id == "default":
        raise InvalidAccountAdmissionError(
            f"Execution intent {intent.execution_intent_id} has missing or invalid bot_id: {bot_id!r}",
            "INVALID_ACCOUNT",
        )
    token = owner_token or f"tok-{uuid.uuid4().hex[:12]}"
    db = mongo_store.get_doc_db()

    def _admit_op(s):
        # Verify bot exists in database
        bot_doc = db["bots"].find_one({"bot_id": bot_id}, session=s)
        if not bot_doc:
            raise InvalidAccountAdmissionError(
                f"Account bot_id {bot_id!r} not found in database",
                "INVALID_ACCOUNT",
            )

        # 1. Validate Policy Decision & Snapshot Freshness
        if policy_decision:
            if policy_decision.disposition not in (
                PolicyDisposition.APPROVE,
                PolicyDisposition.APPROVE_WITH_CAP,
            ):
                raise AdmissionError(
                    f"Policy disposition {policy_decision.disposition} is not approved for admission",
                    "POLICY_NOT_APPROVED",
                )
        if policy_snapshot and getattr(policy_snapshot, "is_degraded", False) and intent.side.upper() == "BUY":
            raise DegradedSnapshotAdmissionError(
                "Degraded snapshot with missing/stale marks cannot admit risk-increasing BUY intents",
                "DEGRADED_SNAPSHOT",
            )

        # 2. Check Idempotency (Request idempotency key)
        existing = db[COLL_EXECUTION_INTENTS].find_one({"idempotency_key": intent.idempotency_key}, session=s)
        if existing:
            existing_intent = ExecutionIntent.model_validate(existing)
            existing_slot = db[COLL_EXECUTION_SLOTS].find_one({"slot_key": slot_key}, session=s)
            existing_resv = db[COLL_RISK_RESERVATIONS].find_one(
                {"execution_intent_id": existing_intent.execution_intent_id}, session=s
            )
            is_consumed = (existing_intent.status == IntentStatus.CONSUMED)
            is_created = (existing_intent.status == IntentStatus.CREATED and (existing_intent.expires_at is None or existing_intent.expires_at > now))

            return {
                "admitted": is_created,
                "is_duplicate": True,
                "already_executed": is_consumed,
                "resumed": is_created,
                "execution_intent": existing_intent,
                "slot": existing_slot or {},
                "reservation": existing_resv or {},
                "owner_token": existing_slot.get("owner_token") if existing_slot else token,
                "status": "CONSUMED" if is_consumed else ("CREATED" if is_created else "SUPERSEDED_OR_EXPIRED"),
            }

        # 3. Claim Execution Slot & Handle Supersession
        slot_col = db[COLL_EXECUTION_SLOTS]
        slot_doc = slot_col.find_one({"slot_key": slot_key}, session=s)
        if slot_doc:
            slot_status = slot_doc.get("status", "ACTIVE")
            slot_expires = slot_doc.get("expires_at")
            is_active = slot_status == "ACTIVE" and (slot_expires is None or slot_expires > now)

            if is_active and slot_doc.get("intent_id") != intent.execution_intent_id:
                if not allow_supersede:
                    raise SlotConflictError(
                        f"Slot {slot_key} is occupied by active intent {slot_doc.get('intent_id')}",
                        "SLOT_CONFLICT",
                    )
                # Check prior intent status
                prior_intent_doc = db[COLL_EXECUTION_INTENTS].find_one(
                    {"execution_intent_id": slot_doc.get("intent_id")}, session=s
                )
                if prior_intent_doc:
                    prior_status = prior_intent_doc.get("status")
                    if prior_status == IntentStatus.CONSUMED.value:
                        raise IntentSupersessionError(
                            f"Prior intent {slot_doc.get('intent_id')} is already CONSUMED and cannot be superseded",
                            "PRIOR_INTENT_ALREADY_CONSUMED",
                        )
                    # Supersede prior intent
                    supersede_execution_intent(
                        slot_doc.get("intent_id"),
                        superseded_by_decision_id=intent.decision_id,
                        session=s,
                    )
                    # Release prior intent's reservation
                    release_risk_reservations_for_intent(slot_doc.get("intent_id"), session=s)

                # CAS update on slot version
                curr_ver = slot_doc.get("version", 1)
                cas_res = slot_col.update_one(
                    {"slot_key": slot_key, "version": curr_ver},
                    {
                        "$set": {
                            "decision_id": intent.decision_id,
                            "intent_id": intent.execution_intent_id,
                            "owner_token": token,
                            "claimed_at": now,
                            "expires_at": intent.expires_at,
                            "status": "ACTIVE",
                        },
                        "$inc": {"version": 1},
                    },
                    session=s,
                )
                if getattr(cas_res, "modified_count", 1) == 0:
                    raise SlotConflictError(f"Concurrent claim race on slot {slot_key}", "SLOT_CAS_FAILED")
                slot_info = slot_col.find_one({"slot_key": slot_key}, session=s)
            else:
                # Slot is expired or released: claim it
                curr_ver = slot_doc.get("version", 1)
                cas_res = slot_col.update_one(
                    {"slot_key": slot_key, "version": curr_ver},
                    {
                        "$set": {
                            "decision_id": intent.decision_id,
                            "intent_id": intent.execution_intent_id,
                            "owner_token": token,
                            "claimed_at": now,
                            "expires_at": intent.expires_at,
                            "status": "ACTIVE",
                        },
                        "$inc": {"version": 1},
                    },
                    session=s,
                )
                if getattr(cas_res, "modified_count", 1) == 0:
                    raise SlotConflictError(f"Concurrent claim race on slot {slot_key}", "SLOT_CAS_FAILED")
                slot_info = slot_col.find_one({"slot_key": slot_key}, session=s)
        else:
            # Slot does not exist: insert new
            new_slot = {
                "slot_key": slot_key,
                "decision_id": intent.decision_id,
                "intent_id": intent.execution_intent_id,
                "owner_token": token,
                "version": 1,
                "claimed_at": now,
                "expires_at": intent.expires_at,
                "status": "ACTIVE",
            }
            try:
                slot_col.insert_one(new_slot, session=s)
                slot_info = new_slot
            except Exception as e:
                raise SlotConflictError(
                    f"Slot {slot_key} concurrent insertion conflict: {e}",
                    "SLOT_CONFLICT",
                ) from e

        # 4. Check & Claim Cash Risk Reservation
        if intent.side.upper() == "BUY":
            cash_avail = float(bot_doc.get("cash_balance", 0.0))
            active_resv_total = get_active_reserved_notional(bot_id, session=s)
            free_cash = cash_avail - active_resv_total
            if required_notional > free_cash:
                raise InsufficientCashReservationError(
                    f"Insufficient free cash ${free_cash:.2f} (balance ${cash_avail:.2f} - reserved ${active_resv_total:.2f}) for required ${required_notional:.2f}",
                    "INSUFFICIENT_CASH_RESERVATION",
                )
            res_bot = db["bots"].update_one(
                {
                    "bot_id": bot_id,
                    "cash_balance": {"$gte": required_notional + active_resv_total},
                },
                {
                    "$inc": {"reservation_version": 1},
                    "$set": {"updated_at": now},
                },
                session=s,
            )
            if res_bot.modified_count == 0:
                raise InsufficientCashReservationError(
                    f"Concurrent reservation conflict or insufficient cash balance for bot {bot_id}",
                    "INSUFFICIENT_CASH_RESERVATION",
                )

        resv = RiskReservation(
            reservation_id=f"resv-{uuid.uuid4().hex[:12]}",
            bot_id=bot_id,
            execution_intent_id=intent.execution_intent_id,
            slot_key=slot_key,
            ticker=intent.ticker.upper().strip(),
            side=intent.side.upper().strip(),
            reserved_notional=required_notional if intent.side.upper() == "BUY" else 0.0,
            status=ReservationStatus.ACTIVE,
            created_at=now,
            expires_at=intent.expires_at,
        )
        db[COLL_RISK_RESERVATIONS].insert_one(resv.model_dump(mode="python"), session=s)

        # 5. Persist Execution Intent
        intent.slot_key = slot_key
        intent.status = IntentStatus.CREATED
        db[COLL_EXECUTION_INTENTS].insert_one(intent.model_dump(mode="python"), session=s)

        try:
            from app.telemetry.trading_adapter import TradingLineageTracker
            TradingLineageTracker.record_reservation(
                cycle_id=intent.cycle_id,
                ticker=intent.ticker,
                reservation_id=resv.reservation_id,
                slot_key=slot_key,
                capital=resv.reserved_notional,
            )
            TradingLineageTracker.record_execution_intent(
                cycle_id=intent.cycle_id,
                ticker=intent.ticker,
                execution_intent_id=intent.execution_intent_id,
                policy_decision_id=intent.policy_decision_id,
                reservation_id=resv.reservation_id,
                action=intent.side,
                shares=int(intent.quantity),
                price=float(intent.limit_price or 0.0),
            )
        except Exception as e:
            logger.debug("[telemetry] admit_execution_intent lineage failed: %s", e)

        return {
            "admitted": True,
            "is_duplicate": False,
            "execution_intent": intent,
            "slot": slot_info,
            "reservation": resv.model_dump(mode="python"),
            "owner_token": token,
        }

    if session is not None:
        return _admit_op(session)
    with mongo_store.with_txn() as s:
        return _admit_op(s)


def expire_stale_intents_and_reservations(cutoff_time: Optional[datetime.datetime] = None) -> dict[str, int]:
    """Marks expired intents, risk reservations, and execution slots as EXPIRED."""
    now = cutoff_time or datetime.datetime.now(datetime.timezone.utc)
    db = mongo_store.get_doc_db()

    # Expire intents
    res_intents = db[COLL_EXECUTION_INTENTS].update_many(
        {"status": IntentStatus.CREATED.value, "expires_at": {"$lt": now}},
        {"$set": {"status": IntentStatus.EXPIRED.value, "expired_at": now}},
    )

    # Expire reservations
    res_resv = db[COLL_RISK_RESERVATIONS].update_many(
        {"status": ReservationStatus.ACTIVE.value, "expires_at": {"$lt": now}},
        {"$set": {"status": ReservationStatus.EXPIRED.value, "expired_at": now}},
    )

    # Expire slots
    res_slots = db[COLL_EXECUTION_SLOTS].update_many(
        {"status": "ACTIVE", "expires_at": {"$lt": now}},
        {"$set": {"status": "EXPIRED", "expired_at": now}},
    )

    return {
        "expired_intents": res_intents.modified_count,
        "expired_reservations": res_resv.modified_count,
        "expired_slots": res_slots.modified_count,
    }

