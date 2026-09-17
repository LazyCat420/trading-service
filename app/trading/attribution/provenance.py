"""Historical Price Provenance and Source-Pinned Observation Service.

Guarantees:
1. Source-Pinned Historical Observations:
   - Evaluates historical decision horizons strictly using source-pinned closed daily bars.
   - Retains exact bar timestamps, data vendor/source, adjustment convention, and deterministic vendor hashes.
2. Calendar-Aware Age Tolerance:
   - Enforces a bounded window (default 5 days for US equities, 2 days for 24/7 crypto) to accommodate
     weekends and market holidays without lookahead.
   - Old-only history beyond the tolerance window returns None and remains explicitly UNRESOLVED.
3. No Current-Quote Substitution:
   - A missing historical horizon bar NEVER falls back to live / current quotes.
   - A valid current quote with a missing historical horizon bar must NOT resolve.
4. Corporate Action & Split Awareness:
   - Detects unadjusted splits / corporate actions and marks them UNADJUSTED to prevent false alpha.
5. Incompatible / Mixed Source Rejection:
   - Horizon observations must match the entry observation's data source.
   - Mixed sources between entry and horizon return None and remain explicitly UNRESOLVED.
6. Deterministic Replay & Versioned Identity:
   - Repeated evaluations query the same maturity cutoff and return identical observations.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import math
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.db import mongo_query, mongo_store
from app.trading.attribution.outcome_contract import (
    AdjustmentConvention,
    HorizonSpec,
    MarketCalendar,
    PriceObservation,
)
from app.utils.tz import ensure_aware

logger = logging.getLogger(__name__)

# Default calendar-aware age tolerances (in calendar days)
DEFAULT_MAX_AGE_DAYS_EQUITY = 5  # Handles standard 2-day weekends + 3/4-day holiday weekends
DEFAULT_MAX_AGE_DAYS_CRYPTO = 2  # 24/7 market: missing bar indicates feed outage


def get_source_pinned_observation(
    ticker: str,
    target_dt: datetime.datetime,
    pinned_source: Optional[str] = None,
    calendar: MarketCalendar = MarketCalendar.US_EQUITY,
    max_age_days: Optional[int] = None,
    adjustment_convention: AdjustmentConvention = AdjustmentConvention.SPLIT_ADJUSTED,
    as_of: Optional[datetime.datetime] = None,
    allow_current_quote_fallback: bool = False,  # Explicitly forbidden for historical horizon evaluation
) -> Optional[PriceObservation]:
    """Retrieves a source-pinned completed daily bar observation within calendar-aware age tolerance.

    Invariants:
    1. Returns None if target_dt has no closed bar within max_age_days (never fabricates prices).
    2. Strictly forbids current-quote fallback for historical horizon evidence.
    3. If pinned_source is provided, bar MUST originate from that exact source; otherwise returns None.
    4. Computes deterministic vendor hash: sha256(source:ticker:date:close:adjustment).
    """
    clean_ticker = ticker.upper().strip()
    target_aware = ensure_aware(target_dt)

    tolerance_days = max_age_days
    if tolerance_days is None:
        tolerance_days = DEFAULT_MAX_AGE_DAYS_CRYPTO if calendar == MarketCalendar.CRYPTO_24_7 else DEFAULT_MAX_AGE_DAYS_EQUITY

    spec = HorizonSpec(calendar=calendar)
    cutoff = spec.closed_bar_cutoff(target_aware)

    # Evaluation cutoff boundary: prevent future leakage
    if as_of is not None:
        eval_cutoff = spec.closed_bar_cutoff(ensure_aware(as_of))
        if cutoff > eval_cutoff:
            # Target horizon is in the future relative to evaluation time
            return None

    earliest_date = cutoff - datetime.timedelta(days=tolerance_days)

    query: dict[str, Any] = {
        "ticker": clean_ticker,
        "close": {"$gt": 0},
        "date": {"$gte": earliest_date, "$lte": cutoff},
    }

    if pinned_source:
        query["source"] = pinned_source

    try:
        # Sort by date desc (closest completed bar to cutoff within tolerance), then source asc
        docs = mongo_store.find_docs(
            "price_history",
            query,
            sort=[("date", -1), ("source", 1)],
            limit=1,
        )
    except Exception as e:
        logger.warning("[provenance] Failed to query price_history for %s: %s", clean_ticker, e)
        return None

    if not docs:
        # Fallback to current quote is explicitly forbidden for historical horizon evaluation
        if allow_current_quote_fallback:
            logger.debug("[provenance] Current quote fallback requested, but historical horizon evaluation forbids it.")
        return None

    row = docs[0]
    try:
        close_px = float(row.get("close") or row.get("price") or 0.0)
        bar_date = ensure_aware(row.get("date") or row.get("timestamp"))
        source = str(row.get("source") or "").strip()
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("[provenance] Invalid row schema for %s: %s", clean_ticker, exc)
        return None

    if close_px <= 0.0 or not math.isfinite(close_px) or not bar_date or not source:
        return None

    # Check for unadjusted corporate actions / split indicators in the record
    adj = adjustment_convention
    if row.get("adjustment_convention") == "UNADJUSTED" or row.get("is_split_adjusted") is False:
        adj = AdjustmentConvention.UNADJUSTED
    elif row.get("adjustment_convention") == "SPLIT_AND_DIVIDEND_ADJUSTED":
        adj = AdjustmentConvention.SPLIT_AND_DIVIDEND_ADJUSTED

    is_delisted = bool(row.get("is_delisted") or row.get("status") == "DELISTED")

    # Generate deterministic vendor hash
    raw_hash_str = f"{source}:{clean_ticker}:{bar_date.isoformat()}:{close_px:.4f}:{adj.value}"
    v_hash = hashlib.sha256(raw_hash_str.encode("utf-8")).hexdigest()[:16]

    return PriceObservation(
        price=close_px,
        date=bar_date,
        source=source,
        bar_type="daily_close",
        adjustment_convention=adj,
        vendor_hash=v_hash,
        is_delisted_or_suspended=is_delisted,
    )
