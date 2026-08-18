from typing import List, Dict, Any
from app.db.connection import get_db
from app.validation.models import ValidationResult, ValidationStatus, QuarantineReason
from app.db import mongo_store
from datetime import datetime, timezone

def save_validation_result(result: ValidationResult):
    """Save the validation result to the database."""
    with get_db() as conn:
        if result.status == ValidationStatus.QUARANTINE:
            mongo_store.update_docs('discovered_tickers', {'ticker': result.ticker}, {'$set': {'validation_status': result.status.value}})
            mongo_store.update_docs('ticker_quarantine', {'ticker': result.ticker}, {'$set': {'reason': result.reason.value if result.reason else "other", 'details': result.details, 'quarantined_at': datetime.now(timezone.utc)}}, upsert=True)
        elif result.status == ValidationStatus.VALID:
            mongo_store.update_docs('discovered_tickers', {'ticker': result.ticker}, {'$set': {'validation_status': result.status.value, 'rate_limited_count': 0}})
        elif result.status == ValidationStatus.PENDING:
            if result.reason == QuarantineReason.RATE_LIMIT_EXCEEDED:
                conn.execute(
                    "UPDATE discovered_tickers SET rate_limited_count = rate_limited_count + 1 WHERE ticker = %s",
                    (result.ticker,)
                )

def get_pending_retries() -> List[str]:
    """Get pending tickers that have not exceeded the rate limit retry count (5)."""
    with get_db() as conn:
        conn.execute(
            "SELECT ticker FROM discovered_tickers WHERE validation_status = 'pending' AND rate_limited_count < 5"
        )
        rows = conn.fetchall()
        return [row[0] for row in rows]

def get_quarantine_summary() -> List[Dict[str, Any]]:
    """Get all quarantined tickers with their reasons."""
    with get_db() as conn:
        conn.execute(
            "SELECT ticker, reason, details, quarantined_at FROM ticker_quarantine ORDER BY quarantined_at DESC"
        )
        rows = conn.fetchall()
        return [
            {
                "ticker": row[0],
                "reason": row[1],
                "details": row[2],
                "quarantined_at": row[3]
            }
            for row in rows
        ]

def release_ticker(ticker: str):
    """Release a ticker from quarantine."""
    with get_db() as conn:
        mongo_store.delete_docs('ticker_quarantine', {'ticker': ticker})
        mongo_store.update_docs('discovered_tickers', {'ticker': ticker}, {'$set': {'validation_status': 'pending', 'rate_limited_count': 0}})

def increment_rate_limit_and_check(ticker: str) -> bool:
    """Increment rate limit count. Return True if it should be quarantined (count >= 5)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE discovered_tickers SET rate_limited_count = rate_limited_count + 1 WHERE ticker = %s RETURNING rate_limited_count",
            (ticker,)
        )
        row = conn.fetchone()
        if row and row[0] >= 5:
            return True
        return False
