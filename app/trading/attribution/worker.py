"""Scheduled Mature Outcome Evaluation and Attribution Worker.

Evaluates mature decision outcomes at their configured horizon (e.g. 7 days)
and evaluates realized FIFO lot closures.
Guarantees:
1. Replayable and idempotent: stable uniqueness keys prevent duplicate records.
2. Missing benchmark data produces a reasoned UNRESOLVED record, never fabricated alpha.
   Scheduled retries ensure resolution after backfill.
3. Distinguishes live (ENFORCE), shadow (SHADOW), and legacy (OBSERVE) outcomes.
4. Uses horizon maturity timestamp for observation rather than processing time.
5. Invokes LotAlphaEvaluator for realized closed tax lots.
6. Paginates and tracks due/unfinished work without starvation.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import Any, Optional

from app.db import mongo_query, mongo_store
from app.trading.attribution.evaluator import DecisionEvaluator, LotAlphaEvaluator
from app.trading.attribution.models import (
    AttributionClass,
    AttributionReport,
    DecisionArtifact,
    DecisionOutcomeRecord,
    OutcomeMaturityStatus,
    PolicyDecision,
    PolicyDisposition,
)
from app.trading.attribution.repository import (
    COLL_ATTRIBUTION_REPORTS,
    COLL_DECISION_ARTIFACTS,
    COLL_EXECUTION_INTENTS,
    COLL_LOT_CLOSURES,
    COLL_POLICY_DECISIONS,
    COLL_POSITION_LOTS,
    expire_stale_intents_and_reservations,
    save_attribution_report,
)
from app.trading.paper_trader import _get_current_price

logger = logging.getLogger(__name__)

COLL_DECISION_OUTCOMES = "decision_outcomes"
COLL_EVALUATION_CHECKPOINTS = "evaluation_checkpoints"


def _get_benchmark_price(symbol: str, target_dt: datetime.datetime) -> Optional[float]:
    """Retrieves the closest closing/market price for benchmark at or near target_dt."""
    from app.quant.returns import one_vendor
    clean_sym = symbol.upper().strip()
    row = mongo_query.find_row(
        "price_history",
        one_vendor(clean_sym, {"ticker": clean_sym, "timestamp": {"$lte": target_dt}}),
        ["close", "price"],
        sort=[("timestamp", -1)],
    )
    if row:
        val = row[0] if row[0] is not None else row[1]
        if val is not None and float(val) > 0:
            return float(val)

    # Fallback to vendor / current quote only if target_dt is recent (within 24 hours of now)
    now = datetime.datetime.now(datetime.timezone.utc)
    if abs((now - target_dt).total_seconds()) < 86400:
        p, _ = _get_current_price(symbol)
        if p and p > 0:
            return float(p)

    return None


def _get_asset_historical_price(ticker: str, target_dt: datetime.datetime) -> Optional[float]:
    """Retrieves the closest closing/market price for ticker at or near target_dt."""
    from app.quant.returns import one_vendor
    clean_sym = ticker.upper().strip()
    row = mongo_query.find_row(
        "price_history",
        one_vendor(clean_sym, {"ticker": clean_sym, "timestamp": {"$lte": target_dt}}),
        ["close", "price"],
        sort=[("timestamp", -1)],
    )
    if row:
        val = row[0] if row[0] is not None else row[1]
        if val is not None and float(val) > 0:
            return float(val)

    # Fallback to current price only if target_dt is within 24 hours of now
    now = datetime.datetime.now(datetime.timezone.utc)
    if abs((now - target_dt).total_seconds()) < 86400:
        p, _ = _get_current_price(clean_sym)
        if p and p > 0:
            return float(p)

    return None


def evaluate_decision_at_horizon(
    artifact: DecisionArtifact,
    now: Optional[datetime.datetime] = None,
) -> Optional[DecisionOutcomeRecord]:
    """Evaluates an individual decision at its declared horizon."""
    eval_time = now or datetime.datetime.now(datetime.timezone.utc)
    entry_time = artifact.created_at
    horizon_days = artifact.declared_horizon_days or 7
    maturity_date = entry_time + datetime.timedelta(days=horizon_days)

    if eval_time < maturity_date:
        # Not mature yet
        return None

    outcome_id = f"out-{artifact.decision_id}-v3"

    # Idempotency check: see if already maturely resolved
    db = mongo_store.get_doc_db()
    existing = db[COLL_DECISION_OUTCOMES].find_one({"outcome_id": outcome_id})
    if existing and existing.get("maturity_status") == OutcomeMaturityStatus.MATURE.value:
        return DecisionOutcomeRecord.model_validate(existing)

    entry_quote = artifact.reference_quote or {}
    p_entry = float(entry_quote.get("price") or 0.0)
    ticker = artifact.ticker.upper().strip()
    action = artifact.requested_action.upper().strip()

    # Get horizon price for asset at maturity_date (not eval_time!)
    p_horizon = _get_asset_historical_price(ticker, maturity_date)
    if not p_horizon or p_horizon <= 0:
        # Asset price missing at maturity: mark unresolved, schedule retry
        outcome = DecisionOutcomeRecord(
            outcome_id=outcome_id,
            decision_id=artifact.decision_id,
            horizon_days=horizon_days,
            benchmark_symbol=artifact.benchmark_symbol or "SPY",
            entry_observation={"price": p_entry, "timestamp": entry_time.isoformat()},
            horizon_observation=None,
            maturity_status=OutcomeMaturityStatus.UNRESOLVED,
            resolved_at=eval_time,
        )
        db[COLL_DECISION_OUTCOMES].update_one(
            {"outcome_id": outcome_id},
            {"$set": outcome.model_dump(mode="python")},
            upsert=True,
        )
        db[COLL_DECISION_ARTIFACTS].update_one(
            {"decision_id": artifact.decision_id},
            {"$set": {"outcome_status": "UNRESOLVED", "retry_after": eval_time + datetime.timedelta(hours=1)}},
        )
        return outcome

    # Benchmark pricing at entry_time and maturity_date
    bm_symbol = artifact.benchmark_symbol or "SPY"
    bm_entry = _get_benchmark_price(bm_symbol, entry_time)
    bm_horizon = _get_benchmark_price(bm_symbol, maturity_date)

    if not bm_entry or not bm_horizon or bm_entry <= 0:
        # Benchmark missing: reasoned UNRESOLVED, schedule retry
        outcome = DecisionOutcomeRecord(
            outcome_id=outcome_id,
            decision_id=artifact.decision_id,
            horizon_days=horizon_days,
            benchmark_symbol=bm_symbol,
            entry_observation={"price": p_entry, "timestamp": entry_time.isoformat()},
            horizon_observation={"price": p_horizon, "timestamp": maturity_date.isoformat()},
            benchmark_entry={"price": bm_entry} if bm_entry else None,
            benchmark_horizon={"price": bm_horizon} if bm_horizon else None,
            maturity_status=OutcomeMaturityStatus.UNRESOLVED,
            resolved_at=eval_time,
        )
        db[COLL_DECISION_OUTCOMES].update_one(
            {"outcome_id": outcome_id},
            {"$set": outcome.model_dump(mode="python")},
            upsert=True,
        )
        db[COLL_DECISION_ARTIFACTS].update_one(
            {"decision_id": artifact.decision_id},
            {"$set": {"outcome_status": "UNRESOLVED", "retry_after": eval_time + datetime.timedelta(hours=1)}},
        )
        return outcome

    # Calculate return and alpha
    metrics = DecisionEvaluator.evaluate(
        entry_price=p_entry,
        horizon_price=p_horizon,
        benchmark_entry=bm_entry,
        benchmark_horizon=bm_horizon,
        action=action,
    )

    outcome = DecisionOutcomeRecord(
        outcome_id=outcome_id,
        decision_id=artifact.decision_id,
        horizon_days=horizon_days,
        benchmark_symbol=bm_symbol,
        entry_observation={"price": p_entry, "timestamp": entry_time.isoformat()},
        horizon_observation={"price": p_horizon, "timestamp": maturity_date.isoformat()},
        benchmark_entry={"price": bm_entry},
        benchmark_horizon={"price": bm_horizon},
        maturity_status=OutcomeMaturityStatus.MATURE,
        decision_return=metrics.decision_return,
        benchmark_return=metrics.benchmark_return,
        decision_alpha=metrics.decision_alpha,
        resolved_at=eval_time,
    )

    db[COLL_DECISION_OUTCOMES].update_one(
        {"outcome_id": outcome_id},
        {"$set": outcome.model_dump(mode="python")},
        upsert=True,
    )

    # Mark artifact as maturely evaluated so it will not starve subsequent decisions
    db[COLL_DECISION_ARTIFACTS].update_one(
        {"decision_id": artifact.decision_id},
        {"$set": {"outcome_status": OutcomeMaturityStatus.MATURE.value, "evaluated_at": eval_time}},
    )

    # Generate Attribution Report separating provenance
    _generate_attribution_report(artifact, outcome)

    return outcome


def _generate_attribution_report(
    artifact: DecisionArtifact,
    outcome: DecisionOutcomeRecord,
) -> AttributionReport:
    """Classifies root cause of performance / failure, separating live, shadow, and legacy provenance."""
    db = mongo_store.get_doc_db()
    rep_id = f"attr-{artifact.decision_id}"

    existing = db[COLL_ATTRIBUTION_REPORTS].find_one({"attribution_id": rep_id})
    if existing:
        return AttributionReport.model_validate(existing)

    alpha = outcome.decision_alpha or 0.0
    classification = AttributionClass.NO_FAILURE
    reason_code = "ALPHA_POSITIVE"
    owner_subsystem = "alpha_engine"

    if outcome.maturity_status == OutcomeMaturityStatus.UNRESOLVED:
        classification = AttributionClass.UNRESOLVED
        reason_code = "MISSING_BENCHMARK_OR_ASSET_QUOTE"
        owner_subsystem = "market_data"
    elif alpha < -2.0:
        classification = AttributionClass.DECISION_FAILURE
        reason_code = "NEGATIVE_DECISION_ALPHA"
        owner_subsystem = "decision_synthesizer"

    # Identify execution provenance from execution intent or policy decision
    provenance = "LIVE"
    effective_mode = "ENFORCE"
    intent_doc = db[COLL_EXECUTION_INTENTS].find_one({"decision_id": artifact.decision_id})
    if intent_doc:
        effective_mode = intent_doc.get("effective_mode", "ENFORCE")
        if effective_mode == "SHADOW":
            provenance = "SHADOW"
        elif effective_mode == "OBSERVE":
            provenance = "LEGACY_OBSERVE"
    else:
        provenance = "UNEXECUTED_OR_SHADOW"

    report = AttributionReport(
        attribution_id=rep_id,
        lineage={
            "decision_id": artifact.decision_id,
            "cycle_id": artifact.cycle_id,
            "outcome_id": outcome.outcome_id,
            "ticker": artifact.ticker,
            "provenance": provenance,
            "effective_mode": effective_mode,
        },
        classification=classification,
        primary_reason_code=reason_code,
        contributing_factors=[
            {"factor": "decision_return", "value": outcome.decision_return},
            {"factor": "benchmark_return", "value": outcome.benchmark_return},
            {"factor": "decision_alpha", "value": alpha},
            {"factor": "provenance", "value": provenance},
        ],
        supporting_record_refs=[artifact.decision_id, outcome.outcome_id],
        confidence=1.0,
        owner_subsystem=owner_subsystem,
    )

    save_attribution_report(report)
    return report


def run_mature_outcome_evaluation_iteration(limit: int = 50) -> int:
    """Finds due, unfinished decisions that have matured beyond their declared horizon and evaluates them."""
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)

    try:
        db["worker_heartbeats"].update_one(
            {"worker": "outcome_worker"},
            {"$set": {"last_heartbeat": now, "status": "RUNNING"}},
            upsert=True,
        )
    except Exception:
        pass

    max_horizon_days = 7
    cutoff = now - datetime.timedelta(days=max_horizon_days)

    # Candidate decisions: matured and not yet marked MATURE, with retry_after <= now or None
    query = {
        "created_at": {"$lte": cutoff},
        "outcome_status": {"$ne": OutcomeMaturityStatus.MATURE.value},
        "$or": [
            {"retry_after": None},
            {"retry_after": {"$lte": now}},
        ],
    }

    docs = list(
        db[COLL_DECISION_ARTIFACTS]
        .find(query)
        .sort("created_at", 1)
        .limit(limit)
    )

    evaluated = 0
    valid_keys = set(DecisionArtifact.model_fields.keys()) | {"_id"}
    for doc in docs:
        try:
            cleaned_doc = {k: v for k, v in doc.items() if k in valid_keys}
            artifact = DecisionArtifact.model_validate(cleaned_doc)
            outcome = evaluate_decision_at_horizon(artifact, now=now)
            if outcome:
                evaluated += 1
        except Exception as exc:
            logger.warning("[OutcomeWorker] Error evaluating artifact %s: %s", doc.get("decision_id"), exc)

    return evaluated


def evaluate_closed_lot_alpha_iteration(limit: int = 50) -> int:
    """Evaluates realized FIFO lot closures with benchmark comparison and attribution."""
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)

    # Un-evaluated closures
    closures = list(
        db[COLL_LOT_CLOSURES]
        .find({
            "alpha_evaluated": {"$ne": True},
            "$or": [
                {"retry_after": None},
                {"retry_after": {"$lte": now}},
            ],
        })
        .sort("closed_at", 1)
        .limit(limit)
    )

    evaluated = 0
    for closure in closures:
        closure_id = closure.get("closure_id")
        try:
            entry_px = float(closure.get("entry_price") or 0.0)
            exit_px = float(closure.get("exit_price") or 0.0)
            ticker = closure.get("ticker", "").upper().strip()
            fees = float(closure.get("fees") or 0.0)
            qty = float(closure.get("closed_qty") or 0.0)
            notional = exit_px * qty
            closed_at = closure.get("closed_at") or now

            # Fetch lot to know opening time and provenance
            lot_id = closure.get("lot_id")
            lot = db[COLL_POSITION_LOTS].find_one({"lot_id": lot_id}) if lot_id else None
            opened_at = lot.get("opened_at") if lot else closed_at
            provenance = lot.get("origin", "LIVE") if lot else "LIVE"
            provenance_complete = lot.get("provenance_complete", True) if lot else True

            bm_symbol = "SPY"
            bm_entry = _get_benchmark_price(bm_symbol, opened_at)
            bm_exit = _get_benchmark_price(bm_symbol, closed_at)

            res = LotAlphaEvaluator.evaluate_lot_closure(
                lot_entry_price=entry_px,
                lot_exit_price=exit_px,
                benchmark_entry=bm_entry,
                benchmark_exit=bm_exit,
                fees=fees,
                notional=notional,
            )

            # Distinguish provenance: if migration or incomplete, flag
            eval_record = {
                "closure_id": closure_id,
                "lot_id": lot_id,
                "bot_id": closure.get("bot_id"),
                "ticker": ticker,
                "provenance": provenance,
                "provenance_complete": provenance_complete,
                "is_attributable": provenance_complete and res.get("status") in ("MATURE", "RESOLVED"),
                "evaluation": res,
                "evaluated_at": now,
            }
            db["lot_closure_evaluations"].update_one(
                {"closure_id": closure_id},
                {"$set": eval_record},
                upsert=True,
            )

            if res.get("status") in ("MATURE", "RESOLVED"):
                db[COLL_LOT_CLOSURES].update_one(
                    {"closure_id": closure_id},
                    {"$set": {"alpha_evaluated": True, "evaluated_at": now, "lot_alpha": res.get("net_alpha", res.get("lot_alpha"))}},
                )
                evaluated += 1
            else:
                # Missing benchmark: retry later
                db[COLL_LOT_CLOSURES].update_one(
                    {"closure_id": closure_id},
                    {"$set": {"retry_after": now + datetime.timedelta(hours=1)}},
                )
        except Exception as exc:
            logger.warning("[OutcomeWorker] Error evaluating closure %s: %s", closure_id, exc)

    return evaluated


async def start_outcome_worker_loop(poll_interval_seconds: float = 30.0) -> None:
    """Background async worker loop for scheduled mature outcome evaluation."""
    logger.info("[OutcomeWorker] Starting background mature outcome evaluation loop...")
    while True:
        try:
            # 1. Expire stale intents and reservations
            expired = expire_stale_intents_and_reservations()
            if any(v > 0 for v in expired.values()):
                logger.info("[OutcomeWorker] Expired stale records: %s", expired)

            # 2. Evaluate mature decision outcomes
            count = run_mature_outcome_evaluation_iteration(limit=50)
            if count > 0:
                logger.info("[OutcomeWorker] Evaluated %d mature decisions", count)

            # 3. Evaluate closed lot alpha
            closures_count = evaluate_closed_lot_alpha_iteration(limit=50)
            if closures_count > 0:
                logger.info("[OutcomeWorker] Evaluated %d closed lot alpha records", closures_count)

            await asyncio.sleep(poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info("[OutcomeWorker] Loop received cancellation — stopping gracefully")
            break
        except Exception as e:
            logger.error("[OutcomeWorker] Loop iteration error: %s", e)
            await asyncio.sleep(poll_interval_seconds)
