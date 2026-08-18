import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


def get_active_directives(limit: int = 10) -> List[dict]:
    """Fetch active directives for injection into the next cycle's prompts.

    This closes the directive loop: directives were previously written every
    cycle and only ever touched again by the janitor's DELETE. Severity-first,
    newest-first.
    """
    try:
        # ORDER BY CASE severity ... , created_at DESC — Mongo cannot sort on a
        # computed rank, so fetch the active set newest-first (the secondary
        # key) and apply the severity rank as a STABLE Python sort, which
        # preserves that ordering within each severity band. LIMIT is applied
        # after the rank, not before: slicing in Mongo would drop a critical
        # directive that happened to be older than `limit` info ones.
        docs = mongo_query.find_rows(
            'cycle_directives', {'status': 'active'},
            ['id', 'directive_type', 'directive_text', 'target_ticker', 'severity'],
            sort=[('created_at', -1)],
        )
        rank = {"critical": 1, "warning": 2}
        rows = sorted(docs, key=lambda r: rank.get(r[4], 3))[:limit]
        return [
            {
                "id": r[0],
                "directive_type": r[1],
                "directive_text": r[2],
                "target_ticker": r[3],
                "severity": r[4],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("[DIRECTIVES] fetch failed (non-fatal): %s", e)
        return []


def _generate_directives(reflection: dict, cycle_id: str, triage_audit: dict) -> None:
    directives_created = 0
    recs = reflection.get("recommendations", [])

    def _emit(**fields) -> None:
        # ON CONFLICT DO NOTHING → insert_only. The id is a fresh uuid per call,
        # so a conflict only happens on a literal replay of the same insert.
        # `created_at` is written explicitly: Postgres defaulted it, Mongo has
        # no column defaults, and get_active_directives() SORTS on it — leaving
        # it unset would make every directive sort as null.
        doc = {"status": "active", "expires_after": 2,
               "created_at": datetime.now(timezone.utc), **fields}
        mongo_store.upsert_doc('cycle_directives', {"id": doc["id"]}, doc,
                               insert_only=True)

    for rec in recs[:3]:
        if not rec or len(rec) < 15: continue
        severity = "info"
        rec_lower = rec.lower()
        if any(w in rec_lower for w in ["critical", "urgent", "immediate", "failing"]):
            severity = "critical"
        elif any(w in rec_lower for w in ["warn", "degrad", "poor", "missing"]):
            severity = "warning"

        _emit(id=f"dir-{uuid.uuid4().hex[:12]}", cycle_id=cycle_id,
              directive_type='recommendation', directive_text=rec[:300],
              severity=severity)
        directives_created += 1

    for issue in triage_audit.get("issues", [])[:3]:
        target_ticker = None
        tickers_list = issue.get("tickers", [])
        if tickers_list: target_ticker = tickers_list[0]
        severity = "warning" if issue["type"] in ("neglect", "over_glancing") else "info"
        _emit(id=f"dir-{uuid.uuid4().hex[:12]}", cycle_id=cycle_id,
              directive_type=f"triage_{issue['type']}",
              directive_text=issue["detail"][:300],
              target_ticker=target_ticker, severity=severity)
        directives_created += 1

    urgent_gaps = reflection.get("urgent_data_gaps", [])
    for ticker in urgent_gaps[:3]:
        _emit(id=f"dir-{uuid.uuid4().hex[:12]}", cycle_id=cycle_id,
              directive_type='data_gap',
              directive_text=f"Critical data gap for {ticker}",
              target_ticker=ticker, severity='warning')
        directives_created += 1

    sched_rec = reflection.get("schedule_recommendation")
    if sched_rec and isinstance(sched_rec, str) and len(sched_rec) >= 10:
        _emit(id=f"dir-{uuid.uuid4().hex[:12]}", cycle_id=cycle_id,
              directive_type='schedule_recommendation',
              directive_text=sched_rec[:300], severity='info')
        directives_created += 1

def _expire_old_directives() -> None:
    try:
        mongo_store.update_docs('cycle_directives',
                                {'status': 'active', 'expires_after': {'$gt': 0}},
                                {'$inc': {'expires_after': -1}})
        mongo_store.update_docs('cycle_directives',
                                {'status': 'active', 'expires_after': {'$lte': 0}},
                                {'$set': {'status': 'expired',
                                          'resolved_at': datetime.now(timezone.utc)}})
    except Exception as e:
        logger.debug("Directive expiry failed: %s", e)
