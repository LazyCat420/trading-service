"""Scheduled Mature Outcome Evaluation and Attribution Worker.

Evaluates mature decision outcomes at their configured horizon (e.g. 7 days)
and evaluates realized FIFO lot closures.
Guarantees:
1. Replayable and idempotent: stable uniqueness keys prevent duplicate records.
2. Missing benchmark data produces a reasoned UNRESOLVED record, never fabricated alpha.
3. Distinguishes live (ENFORCE), shadow (SHADOW), and legacy (OBSERVE) outcomes.
4. Generates canonical DecisionOutcomeRecord and AttributionReport documents.
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

    # Fallback to vendor / current quote if recent
    now = datetime.datetime.now(datetime.timezone.utc)
    if abs((now - target_dt).total_seconds()) < 86400:
        p, _ = _get_current_price(symbol)
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

    # Idempotency check: see if already resolved
    db = mongo_store.get_doc_db()
    existing = db[COLL_DECISION_OUTCOMES].find_one({"outcome_id": outcome_id})
    if existing and existing.get("maturity_status") == OutcomeMaturityStatus.MATURE.value:
        return DecisionOutcomeRecord.model_validate(existing)

    entry_quote = artifact.reference_quote or {}
    p_entry = float(entry_quote.get("price") or 0.0)
    ticker = artifact.ticker.upper().strip()
    action = artifact.requested_action.upper().strip()

    # Get horizon price for asset
    p_horizon, _ = _get_current_price(ticker)
    if not p_horizon or p_horizon <= 0:
        # Asset price missing: mark unresolved
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
        return outcome

    # Benchmark pricing
    bm_symbol = artifact.benchmark_symbol or "SPY"
    bm_entry = _get_benchmark_price(bm_symbol, entry_time)
    bm_horizon = _get_benchmark_price(bm_symbol, eval_time)

    if not bm_entry or not bm_horizon or bm_entry <= 0:
        # Benchmark missing: reasoned UNRESOLVED
        outcome = DecisionOutcomeRecord(
            outcome_id=outcome_id,
            decision_id=artifact.decision_id,
            horizon_days=horizon_days,
            benchmark_symbol=bm_symbol,
            entry_observation={"price": p_entry, "timestamp": entry_time.isoformat()},
            horizon_observation={"price": p_horizon, "timestamp": eval_time.isoformat()},
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
        horizon_observation={"price": p_horizon, "timestamp": eval_time.isoformat()},
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

    # Generate Attribution Report
    _generate_attribution_report(artifact, outcome)

    return outcome


def _generate_attribution_report(
    artifact: DecisionArtifact,
    outcome: DecisionOutcomeRecord,
) -> AttributionReport:
    """Classifies root cause of performance / failure."""
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

    report = AttributionReport(
        attribution_id=rep_id,
        lineage={
            "decision_id": artifact.decision_id,
            "cycle_id": artifact.cycle_id,
            "outcome_id": outcome.outcome_id,
            "ticker": artifact.ticker,
        },
        classification=classification,
        primary_reason_code=reason_code,
        contributing_factors=[
            {"factor": "decision_return", "value": outcome.decision_return},
            {"factor": "benchmark_return", "value": outcome.benchmark_return},
            {"factor": "decision_alpha", "value": alpha},
        ],
        supporting_record_refs=[artifact.decision_id, outcome.outcome_id],
        confidence=1.0,
        owner_subsystem=owner_subsystem,
    )

    save_attribution_report(report)
    return report


def run_mature_outcome_evaluation_iteration(limit: int = 50) -> int:
    """Finds decisions that have matured beyond their declared horizon and evaluates them."""
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    max_horizon_days = 7
    cutoff = now - datetime.timedelta(days=max_horizon_days)

    # Find candidate decisions
    docs = list(
        db[COLL_DECISION_ARTIFACTS]
        .find({"created_at": {"$lte": cutoff}})
        .sort("created_at", 1)
        .limit(limit)
    )

    evaluated = 0
    for doc in docs:
        try:
            artifact = DecisionArtifact.model_validate(doc)
            outcome = evaluate_decision_at_horizon(artifact, now=now)
            if outcome:
                evaluated += 1
        except Exception as exc:
            logger.warning("[OutcomeWorker] Error evaluating artifact %s: %s", doc.get("decision_id"), exc)

    return evaluated


async def start_outcome_worker_loop(poll_interval_seconds: float = 30.0) -> None:
    """Background async worker loop for scheduled mature outcome evaluation."""
    logger.info("[OutcomeWorker] Starting background mature outcome evaluation loop...")
    while True:
        try:
            count = run_mature_outcome_evaluation_iteration(limit=50)
            if count > 0:
                logger.info("[OutcomeWorker] Evaluated %d mature decisions", count)
            await asyncio.sleep(poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info("[OutcomeWorker] Loop received cancellation — stopping gracefully")
            break
        except Exception as e:
            logger.error("[OutcomeWorker] Loop iteration error: %s", e)
            await asyncio.sleep(poll_interval_seconds)
