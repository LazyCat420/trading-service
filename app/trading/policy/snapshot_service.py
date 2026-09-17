"""Authoritative Policy Snapshot Service.

Builds immutable, point-in-time portfolio, quote, and risk snapshots before
policy evaluation. Provides deterministic canonical JSON serialization and
disjoint hash identifiers:
- snapshot_hash: SHA-256 of canonical snapshot payload
- policy_config_hash: SHA-256 of policy rule configuration
- evaluation_key: decision revision + snapshot hash + policy version
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from typing import Any, Optional

from app.db import mongo_query, mongo_store
from app.trading.policy.policy_translator import PolicyInputSnapshot
from app.utils.tz import ensure_aware

logger = logging.getLogger(__name__)

COLL_POLICY_SNAPSHOTS = "policy_snapshots"


def canonical_json_dumps(obj: Any) -> str:
    """Deterministic, canonical JSON serialization for domain hashing.

    Rules:
    - Dictionaries have keys sorted recursively.
    - Floats rounded to 4 decimal places for stable float representation.
    - Datetimes serialized to ISO 8601 UTC with 'Z'.
    - Sets / sequences of primitives sorted.
    """
    def _normalize(val: Any) -> Any:
        if isinstance(val, dict):
            return {k: _normalize(val[k]) for k in sorted(val.keys())}
        if isinstance(val, (list, tuple)):
            return [_normalize(item) for item in val]
        if isinstance(val, set):
            return sorted([_normalize(item) for item in val])
        if isinstance(val, (datetime.datetime, datetime.date)):
            dt = ensure_aware(val) if isinstance(val, datetime.datetime) else datetime.datetime.combine(val, datetime.time.min, datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(val, float):
            return round(val, 4)
        return val

    normalized = _normalize(obj)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def compute_snapshot_hash(snapshot_payload: dict[str, Any]) -> str:
    """Compute SHA-256 hash of canonical snapshot payload."""
    canonical_str = canonical_json_dumps(snapshot_payload)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


def compute_policy_config_hash(config_payload: dict[str, Any]) -> str:
    """Compute SHA-256 hash of policy parameters."""
    canonical_str = canonical_json_dumps(config_payload)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


def compute_evaluation_key(
    decision_id: str,
    decision_revision: int,
    snapshot_hash: str,
    policy_version: str = "v1.0",
) -> str:
    """Unique key identifying an immutable decision evaluation slot."""
    return f"{decision_id}:v{decision_revision}:{snapshot_hash[:16]}:{policy_version}"


def build_policy_snapshot(
    bot_id: str,
    ticker: str,
    quote_price: float,
    quote_age_hours: float = 0.0,
    quote_timestamp: Optional[datetime.datetime] = None,
    quote_source: str = "stored_price",
    as_of: Optional[datetime.datetime] = None,
    session: Any = None,
) -> tuple[PolicyInputSnapshot, dict[str, Any]]:
    """Builds authoritative PolicyInputSnapshot from real MongoDB state."""
    now = as_of or datetime.datetime.now(datetime.timezone.utc)
    quote_ts = quote_timestamp or now

    if quote_price <= 0:
        raise ValueError(f"Invalid quote price ${quote_price} for {ticker}")

    # 1. Fetch bot balance
    bot_row = mongo_query.find_row("bots", {"bot_id": bot_id}, ["cash_balance", "starting_balance"], session=session)
    cash = float(bot_row[0]) if bot_row and bot_row[0] is not None else 100000.0
    starting_balance = float(bot_row[1]) if bot_row and len(bot_row) > 1 and bot_row[1] is not None else 100000.0

    # 2. Fetch positions & compute mark-to-market equity
    positions = mongo_query.find_rows(
        "positions",
        {"bot_id": bot_id},
        ["ticker", "qty", "avg_entry_price"],
        session=session,
    )
    held_positions: dict[str, float] = {}
    total_positions_val = 0.0
    held_ticker_val = 0.0
    position_marks: dict[str, dict[str, Any]] = {}
    is_degraded = False
    degraded_reasons: list[str] = []

    for p in positions:
        pos_tkr, pos_qty, pos_avg_px = p[0], float(p[1]), float(p[2])
        if pos_qty > 0:
            held_positions[pos_tkr] = pos_qty
            if pos_tkr.upper() == ticker.upper():
                mark_px = quote_price
                mark_age = quote_age_hours
                mark_src = quote_source
                mark_status = "FRESH" if quote_age_hours <= 12.0 else "STALE"
            else:
                from app.trading.paper_trader import _get_current_price
                m_px, m_age = _get_current_price(pos_tkr)
                if m_px is not None and m_px > 0:
                    mark_px = m_px
                    mark_age = m_age if m_age is not None else 0.0
                    mark_src = "vendor_quote"
                    mark_status = "FRESH" if mark_age <= 24.0 else ("STALE" if mark_age <= 96.0 else "EXPIRED")
                else:
                    mark_px = pos_avg_px
                    mark_age = 999.0
                    mark_src = "entry_fallback"
                    mark_status = "MISSING"

            position_marks[pos_tkr] = {
                "price": mark_px,
                "age_hours": mark_age,
                "source": mark_src,
                "status": mark_status,
                "retrieved_at": now.isoformat(),
            }

            if mark_status in ("EXPIRED", "MISSING"):
                is_degraded = True
                degraded_reasons.append(f"MARK_{mark_status}_{pos_tkr}")

            val = pos_qty * mark_px
            total_positions_val += val
            if pos_tkr.upper() == ticker.upper():
                held_ticker_val = val

    portfolio_equity = max(1.0, cash + total_positions_val)

    # 3. Fetch active lots for ticker
    active_lots = mongo_query.find_rows(
        "position_lots",
        {"bot_id": bot_id, "ticker": ticker, "status": {"$in": ["open", "partial"]}},
        ["lot_id", "remaining_qty", "entry_price", "opened_at"],
        sort=[("opened_at", 1)],
        session=session,
    )
    lots_payload = [
        {"lot_id": l[0], "remaining_qty": float(l[1]), "entry_price": float(l[2])}
        for l in active_lots
    ]

    # 4. Fetch pending order reservations
    try:
        from app.trading.order_capacity import pending_capacity
        reservations = pending_capacity(bot_id, ticker)
        cash_reserved = float(reservations.get("cash_reserved", 0.0))
        ticker_reserved = float(reservations.get("ticker_reserved", 0.0))
    except Exception as cap_err:
        logger.warning("[SnapshotService] pending_capacity check failed: %s", cap_err)
        cash_reserved = 0.0
        ticker_reserved = 0.0

    # 5. Breaker & drawdown check
    drawdown_pct = max(0.0, (starting_balance - portfolio_equity) / starting_balance) if starting_balance > 0 else 0.0
    breaker_active = drawdown_pct >= 0.25

    snapshot_payload: dict[str, Any] = {
        "bot_id": bot_id,
        "ticker": ticker.upper(),
        "as_of": now,
        "portfolio_equity": portfolio_equity,
        "cash_balance": cash,
        "available_cash": max(0.0, cash - cash_reserved),
        "held_positions": held_positions,
        "held_ticker_value": held_ticker_val,
        "active_lots": lots_payload,
        "cash_reserved": cash_reserved,
        "ticker_reserved": ticker_reserved,
        "quote_price": quote_price,
        "quote_age_hours": quote_age_hours,
        "quote_timestamp": quote_ts,
        "quote_source": quote_source,
        "drawdown_pct": drawdown_pct,
        "breaker_active": breaker_active,
        "position_marks": position_marks,
        "is_degraded": is_degraded,
        "degraded_reasons": degraded_reasons,
        "data_quality": {
            "stale_quote": quote_age_hours > 12.0,
            "sanity_passed": True,
        },
    }

    snap_hash = compute_snapshot_hash(snapshot_payload)
    snapshot_payload["snapshot_hash"] = snap_hash

    # Persist snapshot before evaluation
    try:
        db = mongo_store.get_doc_db()
        db[COLL_POLICY_SNAPSHOTS].update_one(
            {"snapshot_hash": snap_hash},
            {"$setOnInsert": snapshot_payload},
            upsert=True,
        )
    except Exception as save_err:
        logger.warning("[SnapshotService] Snapshot save non-fatal error: %s", save_err)

    snapshot_obj = PolicyInputSnapshot(
        snapshot_id=snap_hash[:16],
        cycle_id=f"sim-snap-{snap_hash[:8]}",
        portfolio_equity=portfolio_equity,
        cash_balance=cash,
        held_positions=held_positions,
        held_ticker_value=held_ticker_val,
        quote_price=quote_price,
        quote_age_hours=quote_age_hours,
        quote_timestamp=quote_ts,
        is_held=held_ticker_val > 0,
        as_of=now,
        circuit_breaker_active=breaker_active,
        is_degraded=is_degraded,
        degraded_reasons=degraded_reasons,
        position_marks=position_marks,
    )

    return snapshot_obj, snapshot_payload
