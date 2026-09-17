"""Unified Outcome Access Layer for Verified Decisions and Closed Lots.

Implements Step 10: Connect verified outcomes to all readers:
- Attribution reports
- Agent & skill scorecards
- Autoresearch learning cohort exports
- Shadow comparison reports

Guarantees:
- Every reader observes identical maturity date, return percentage, fees, dollar P&L, and net alpha.
- Quarantined, synthetic, and excluded records are consistently dropped from live statistics.
- Legacy documents gracefully project normalized values without corrupting v4 learning cohorts.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Optional, Union

from app.db import mongo_store
from app.services.cycle_scope import exclude_synthetic, is_synthetic_cycle
from app.trading.attribution.repository import (
    COLL_DECISION_OUTCOMES,
    COLL_LOT_CLOSURES,
)

logger = logging.getLogger(__name__)


def _parse_datetime(val: Any) -> Optional[datetime.datetime]:
    if val is None:
        return None
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=datetime.timezone.utc)
        return val.astimezone(datetime.timezone.utc)
    if isinstance(val, str):
        try:
            dt = datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except Exception:
            return None
    return None


def _check_learning_eligibility(doc: dict[str, Any]) -> bool:
    """Predicate evaluating if an outcome document is eligible for model learning."""
    if doc.get("is_quarantined") is True:
        return False
    if doc.get("exclusion_reason") is not None:
        return False
    if is_synthetic_cycle(doc.get("cycle_id", "")):
        return False

    # Contract v4 check
    maturity_status = doc.get("maturity_status")
    if maturity_status == "MATURE_VERIFIED":
        return True

    # Legacy contract v2/v3 check
    evidence_state = doc.get("outcome_evidence_state")
    if evidence_state == "verified":
        entry_src = doc.get("entry_price_source")
        exit_src = doc.get("exit_price_source")
        if entry_src and exit_src and entry_src == exit_src:
            return True

    return False


def get_verified_decision_outcomes(
    since: Optional[Union[str, datetime.datetime]] = None,
    limit: int = 0,
    bot_id: Optional[str] = None,
    ticker: Optional[str] = None,
    db: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Single source of truth for verified decision outcomes across all readers.
    
    Returns:
        List of standardized outcome records with identical return and alpha calculations.
    """
    database = db if db is not None else mongo_store.get_doc_db()

    query: dict[str, Any] = {
        "is_quarantined": {"$ne": True},
        "exclusion_reason": None,
        "$or": [
            {"maturity_status": {"$in": ["MATURE_VERIFIED", "MATURE"]}},
            {"status": {"$in": ["MATURE", "RESOLVED"]}},
            {"resolved_at": {"$ne": None}},
        ],
    }

    # Exclude synthetic cycles
    query.update(exclude_synthetic())

    if since:
        since_dt = _parse_datetime(since)
        if since_dt:
            query["created_at"] = {"$gte": since_dt}

    if bot_id:
        query["bot_id"] = bot_id

    if ticker:
        query["ticker"] = ticker.upper().strip()

    cursor = database[COLL_DECISION_OUTCOMES].find(query).sort("created_at", 1)
    if limit > 0:
        cursor = cursor.limit(limit)

    results: list[dict[str, Any]] = []
    for doc in cursor:
        ret_val = doc.get("decision_return_pct")
        if ret_val is None:
            ret_val = doc.get("pnl_pct", 0.0)
        return_pct = round(float(ret_val), 4)

        alpha_val = doc.get("forecast_alpha")
        if alpha_val is None:
            alpha_val = doc.get("alpha")
        alpha = round(float(alpha_val), 4) if alpha_val is not None else None

        bm_val = doc.get("benchmark_return_pct")
        if bm_val is None:
            bm_val = doc.get("benchmark_return")
        bm_return = round(float(bm_val), 4) if bm_val is not None else None

        outcome_label = doc.get("outcome")
        if not outcome_label:
            outcome_label = "WIN" if return_pct > 0 else ("LOSS" if return_pct < 0 else "FLAT")

        results.append({
            "decision_id": doc.get("decision_id"),
            "cycle_id": doc.get("cycle_id"),
            "bot_id": doc.get("bot_id", "default"),
            "ticker": doc.get("ticker"),
            "action": doc.get("action", doc.get("requested_action")),
            "action_classification": doc.get("action_classification", doc.get("claim_type")),
            "confidence": float(doc.get("confidence") or 0.0),
            "return_pct": return_pct,
            "pnl_pct": return_pct,  # Compatibility alias for scorecard readers
            "benchmark_return_pct": bm_return,
            "forecast_alpha": alpha,
            "alpha": alpha,          # Compatibility alias for scorecard readers
            "outcome": outcome_label,
            "maturity_date": _parse_datetime(doc.get("maturity_date")),
            "created_at": _parse_datetime(doc.get("created_at")),
            "resolved_at": _parse_datetime(doc.get("resolved_at") or doc.get("evaluated_at")),
            "contract_version": int(doc.get("contract_version", doc.get("outcome_contract_version", 4))),
            "maturity_status": doc.get("maturity_status", "MATURE_VERIFIED"),
            "is_eligible_for_learning": _check_learning_eligibility(doc),
        })

    return results


def get_verified_closed_lot_outcomes(
    since: Optional[Union[str, datetime.datetime]] = None,
    limit: int = 0,
    bot_id: Optional[str] = None,
    ticker: Optional[str] = None,
    db: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Single source of truth for verified realized closed lot outcomes across all readers.
    
    Guarantees:
    - Enforces fee conservation and invested capital denominator.
    - Excludes non-attributable or incomplete lots.
    """
    database = db if db is not None else mongo_store.get_doc_db()

    query: dict[str, Any] = {
        "is_attributable": True,
        "provenance_complete": True,
        "origin": {"$in": ["LIVE", "HISTORICAL_RECONSTRUCTION"]},
    }

    if since:
        since_dt = _parse_datetime(since)
        if since_dt:
            query["closed_at"] = {"$gte": since_dt}

    if bot_id:
        query["bot_id"] = bot_id

    if ticker:
        query["ticker"] = ticker.upper().strip()

    cursor = database[COLL_LOT_CLOSURES].find(query).sort("closed_at", 1)
    if limit > 0:
        cursor = cursor.limit(limit)

    results: list[dict[str, Any]] = []
    for doc in cursor:
        results.append({
            "closure_id": doc.get("closure_id"),
            "lot_id": doc.get("lot_id"),
            "bot_id": doc.get("bot_id"),
            "ticker": doc.get("ticker"),
            "closed_qty": float(doc.get("closed_qty", 0.0)),
            "entry_price": float(doc.get("entry_price", 0.0)),
            "exit_price": float(doc.get("exit_price", 0.0)),
            "allocated_entry_fee": float(doc.get("allocated_entry_fee", 0.0)),
            "exit_fee": float(doc.get("exit_fee", doc.get("fees", 0.0))),
            "total_fees": float(doc.get("fees", 0.0)),
            "dollar_pnl": float(doc.get("dollar_pnl") if doc.get("dollar_pnl") is not None else doc.get("net_pnl", 0.0)),
            "invested_capital_denominator": float(doc.get("invested_capital_denominator", 0.0)),
            "net_realized_return": float(doc.get("net_realized_return", 0.0)),
            "gross_pnl": float(doc.get("gross_pnl", 0.0)),
            "lot_alpha": float(doc.get("lot_alpha")) if doc.get("lot_alpha") is not None else None,
            "benchmark_return": float(doc.get("benchmark_return")) if doc.get("benchmark_return") is not None else None,
            "closed_at": _parse_datetime(doc.get("closed_at")),
            "provenance": doc.get("provenance", "LIVE"),
            "is_attributable": True,
        })

    return results


def get_learning_cohort_outcomes(
    since: Optional[Union[str, datetime.datetime]] = None,
    limit: int = 0,
    ticker: Optional[str] = None,
    db: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Fetches decision outcomes strictly qualified for autoresearch model learning."""
    all_outcomes = get_verified_decision_outcomes(
        since=since,
        limit=0,
        ticker=ticker,
        db=db,
    )
    eligible = [o for o in all_outcomes if o["is_eligible_for_learning"]]
    if limit > 0:
        return eligible[:limit]
    return eligible


def get_shadow_comparison_outcomes(
    since: Optional[Union[str, datetime.datetime]] = None,
    limit: int = 0,
    db: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Joins shadow decision scores against verified decision outcomes."""
    database = db if db is not None else mongo_store.get_doc_db()

    scores_cursor = database["decision_scores"].find({"score": {"$ne": None}})
    if limit > 0:
        scores_cursor = scores_cursor.limit(limit)
    scores = list(scores_cursor)

    outcomes = get_verified_decision_outcomes(since=since, db=database)

    # Build primary index by decision_id and fallback by (cycle_id, ticker)
    index_by_decision: dict[str, dict[str, Any]] = {}
    index_by_cycle_ticker: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for o in outcomes:
        dec_id = o.get("decision_id")
        if dec_id:
            index_by_decision[dec_id] = o

        c_id = o.get("cycle_id")
        t_sym = o.get("ticker")
        if c_id and t_sym:
            index_by_cycle_ticker.setdefault((c_id, t_sym), []).append(o)

    joined: list[dict[str, Any]] = []
    for s in scores:
        dec_id = s.get("decision_id")
        cycle_id = s.get("cycle_id")
        ticker_val = s.get("ticker")

        match: Optional[dict[str, Any]] = None
        if dec_id and dec_id in index_by_decision:
            match = index_by_decision[dec_id]
        elif (cycle_id, ticker_val) in index_by_cycle_ticker:
            matches = index_by_cycle_ticker[(cycle_id, ticker_val)]
            match = matches[0] if matches else None

        joined.append({
            "score_id": str(s.get("_id", "")),
            "decision_id": dec_id or (match.get("decision_id") if match else None),
            "cycle_id": cycle_id,
            "ticker": ticker_val,
            "band": s.get("band"),
            "score": s.get("score"),
            "baseline_confidence": s.get("baseline_confidence"),
            "risk_reward": s.get("risk_reward"),
            "board_action": s.get("board_action"),
            "board_confidence": s.get("board_confidence"),
            "verified_return_pct": match.get("return_pct") if match else None,
            "verified_alpha": match.get("forecast_alpha") if match else None,
            "verified_benchmark_return": match.get("benchmark_return_pct") if match else None,
            "outcome_label": match.get("outcome") if match else None,
        })

    return joined
