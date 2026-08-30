"""
Candidate Validation Persistence — handles discovered ticker quarantine and retry tracking.

Pure MongoDB implementation for discovered_tickers and ticker_quarantine collections.
"""

from typing import List, Dict, Any
from datetime import datetime, timezone

from app.validation.models import ValidationResult, ValidationStatus, QuarantineReason
from app.db import mongo_store, mongo_query


def save_validation_result(result: ValidationResult) -> None:
    """Save the validation result to MongoDB."""
    if result.status == ValidationStatus.QUARANTINE:
        mongo_store.update_docs(
            'discovered_tickers',
            {'ticker': result.ticker},
            {'$set': {'validation_status': result.status.value}}
        )
        mongo_store.update_docs(
            'ticker_quarantine',
            {'ticker': result.ticker},
            {'$set': {
                'reason': result.reason.value if result.reason else "other",
                'details': result.details,
                'quarantined_at': datetime.now(timezone.utc),
            }},
            upsert=True
        )
    elif result.status == ValidationStatus.VALID:
        mongo_store.update_docs(
            'discovered_tickers',
            {'ticker': result.ticker},
            {'$set': {'validation_status': result.status.value, 'rate_limited_count': 0}}
        )
    elif result.status == ValidationStatus.PENDING:
        if result.reason == QuarantineReason.RATE_LIMIT_EXCEEDED:
            mongo_store.update_docs(
                'discovered_tickers',
                {'ticker': result.ticker},
                {'$inc': {'rate_limited_count': 1}}
            )


def get_pending_retries() -> List[str]:
    """Get pending tickers that have not exceeded the rate limit retry count (5).

    A ticker that has NEVER been rate-limited must be in this list. Under
    Postgres `rate_limited_count` was `DEFAULT 0`, so `< 5` matched every
    pending row. No Mongo writer supplies it, and `{"$lt": 5}` does not match a
    document where the field is absent — so this returned 0 of the 98 pending
    tickers, and the only tickers it could ever return were ones that had
    already been rate-limited at least once.

    Same defect the `validation_status` `$setOnInsert` calls in
    reddit_collector/youtube_collector were added to fix; that sweep fixed one
    column of the class rather than the class. The writers below now seed
    `rate_limited_count`, and this read tolerates its absence so the documents
    already in the store work without a backfill.
    """
    docs = mongo_store.find_docs(
        'discovered_tickers',
        {'validation_status': 'pending',
         # `{field: None}` matches BOTH a null and a missing field; `$lt`
         # matches neither.
         '$or': [{'rate_limited_count': {'$lt': 5}},
                 {'rate_limited_count': None}]},
        projection={'ticker': 1, '_id': 0}
    )
    return [d['ticker'] for d in docs if d.get('ticker')]


def get_quarantine_summary() -> List[Dict[str, Any]]:
    """Get all quarantined tickers with their reasons."""
    docs = mongo_store.find_docs('ticker_quarantine', {}, sort=[('quarantined_at', -1)])
    return [
        {
            "ticker": d.get("ticker"),
            "reason": d.get("reason"),
            "details": d.get("details"),
            "quarantined_at": d.get("quarantined_at"),
        }
        for d in docs
    ]


def release_ticker(ticker: str) -> None:
    """Release a ticker from quarantine."""
    mongo_store.delete_docs('ticker_quarantine', {'ticker': ticker})
    mongo_store.update_docs(
        'discovered_tickers',
        {'ticker': ticker},
        {'$set': {'validation_status': 'pending', 'rate_limited_count': 0}}
    )


def increment_rate_limit_and_check(ticker: str) -> bool:
    """Increment rate limit count. Return True if it should be quarantined (count >= 5)."""
    mongo_store.update_docs(
        'discovered_tickers',
        {'ticker': ticker},
        {'$inc': {'rate_limited_count': 1}}
    )
    docs = mongo_store.find_docs('discovered_tickers', {'ticker': ticker}, limit=1)
    if docs and (docs[0].get('rate_limited_count') or 0) >= 5:
        return True
    return False
