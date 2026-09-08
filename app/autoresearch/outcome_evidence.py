"""Price-reference outcomes eligible for learning; execution P&L is a separate ledger.

Legacy rows are retained but cannot claim the new contract by merely being old enough.
Entry and exit must name the same source and dated, completed daily bars. No historical
row is silently upgraded from its stored win/loss label.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo

from app.db import mongo_store
from app.services.cycle_scope import exclude_synthetic, is_synthetic_cycle
from app.utils.tz import ensure_aware

CONTRACT_VERSION = 2
HORIZON_DAYS = 7
GRACE_DAYS = 5


def learning_query() -> dict:
    """Only the resolver below may stamp verified v2 price-pair provenance."""
    return {**exclude_synthetic(), 'outcome_contract_version': CONTRACT_VERSION,
            'outcome_evidence_state': 'verified', 'horizon_days': HORIZON_DAYS,
            'claim_type': {'$in': ['immediate_directional', 'flat_wait']},
            'entry_price': {'$gt': 0}, 'exit_price': {'$gt': 0},
            'entry_date': {'$type': 'date'}, 'exit_date': {'$type': 'date'},
            'entry_price_source': {'$type': 'string', '$ne': ''},
            '$expr': {'$eq': ['$entry_price_source', '$exit_price_source']}}


def claim_type(action: str, decision: dict) -> str | None:
    """Only claims with an unambiguous current-price interpretation are graded.

    Conditional entries need trigger-path evidence. A held-position HOLD is a
    KEEP decision, not an avoided-decline forecast. Preserve both for separate
    evaluation instead of teaching the flat-wait score from incompatible claims.
    """
    if action in ('BUY', 'SELL') and decision.get('entry_mode') == 'enter_now':
        return 'immediate_directional'
    if action == 'HOLD' and decision.get('hold_reason_held') is False:
        return 'flat_wait'
    return None


def closed_bar_cutoff(as_of: datetime) -> datetime:
    """Conservative US equity daily-close availability, without an intraday oracle.

    Before 16:15 New York time use the previous calendar date. Missing weekends
    and holidays are handled by the stored bars and the bounded lookup. This
    contract describes a daily reference price, never an executable live quote.
    """
    at = ensure_aware(as_of)
    local = at.astimezone(ZoneInfo('America/New_York'))
    date = local.date()
    if (local.hour, local.minute) < (16, 15):
        date -= timedelta(days=1)
    return datetime.combine(date, datetime.min.time(), tzinfo=timezone.utc)


def _observation(row: dict | None) -> dict | None:
    if not row or not isinstance(row.get('source'), str) or not row['source'].strip():
        return None
    try:
        value = float(row['close'])
        date = ensure_aware(row['date'])
    except (KeyError, TypeError, ValueError):
        return None
    if not date or not math.isfinite(value) or value <= 0:
        return None
    return {'price': value, 'date': date, 'source': row['source']}


def entry_observation(ticker: str, as_of: datetime) -> dict | None:
    from app.config.config_tickers import classify_asset
    if classify_asset(ticker) != 'stock':
        return None  # Other sessions require a separate price-availability contract.
    cutoff = closed_bar_cutoff(as_of)
    rows = mongo_store.find_docs('price_history', {
        'ticker': ticker, 'close': {'$gt': 0}, 'source': {'$type': 'string', '$ne': ''},
        'date': {'$gte': cutoff - timedelta(days=GRACE_DAYS), '$lte': cutoff},
    }, sort=[('date', -1), ('source', 1)], limit=1)
    # Tie-breaking is deterministic at this observation time. The selected
    # source is then pinned, not reselected after seeing the future exit price.
    return _observation(rows[0]) if rows else None


def horizon_date(decision_as_of: datetime) -> datetime:
    at = ensure_aware(decision_as_of)
    date = at.date() + timedelta(days=HORIZON_DAYS)
    return datetime.combine(date, datetime.min.time(), tzinfo=timezone.utc)


def exit_observation(ticker: str, decision_as_of: datetime, source: str,
                     *, as_of: datetime | None = None) -> dict | None:
    if not isinstance(source, str) or not source:
        return None
    target = horizon_date(decision_as_of)
    end = min(target + timedelta(days=GRACE_DAYS), closed_bar_cutoff(as_of or datetime.now(timezone.utc)))
    if end < target:
        return None
    rows = mongo_store.find_docs('price_history', {
        'ticker': ticker, 'source': source, 'close': {'$gt': 0},
        'date': {'$gte': target, '$lte': end},
    }, sort=[('date', 1)], limit=1)
    return _observation(rows[0]) if rows else None


def verified_pair(row: dict, exit_ref: dict, *, as_of: datetime | None = None) -> bool:
    """Validate independently of a database match before grading or memory writeback."""
    if (row.get('outcome_contract_version') != CONTRACT_VERSION
        or row.get('claim_type') not in ('immediate_directional', 'flat_wait')
        or is_synthetic_cycle(row.get('cycle_id'))):
        return False
    try:
        at = ensure_aware(row['decision_as_of'])
        entry_date = ensure_aware(row['entry_date'])
        exit_date = ensure_aware(exit_ref['date'])
        price = float(row['entry_price']); end_price = float(exit_ref['price'])
        source = row['entry_price_source']
        target = horizon_date(at)
        return bool(isinstance(source, str) and source and source == exit_ref['source']
          and math.isfinite(price) and price > 0 and math.isfinite(end_price) and end_price > 0
          and closed_bar_cutoff(at) - timedelta(days=GRACE_DAYS) <= entry_date <= closed_bar_cutoff(at)
          and target <= exit_date <= target + timedelta(days=GRACE_DAYS)
          and exit_date <= closed_bar_cutoff(as_of or datetime.now(timezone.utc)))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
