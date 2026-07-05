import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def _generate_directives(reflection: dict, cycle_id: str, triage_audit: dict) -> None:
    directives_created = 0
    recs = reflection.get("recommendations", [])
    with get_db() as db:
        for rec in recs[:3]:
            if not rec or len(rec) < 15: continue
            severity = "info"
            rec_lower = rec.lower()
            if any(w in rec_lower for w in ["critical", "urgent", "immediate", "failing"]):
                severity = "critical"
            elif any(w in rec_lower for w in ["warn", "degrad", "poor", "missing"]):
                severity = "warning"

            directive_id = f"dir-{uuid.uuid4().hex[:12]}"
            db.execute(
                """INSERT INTO cycle_directives (id, cycle_id, directive_type, directive_text, severity, status, expires_after)
                VALUES (%s, %s, 'recommendation', %s, %s, 'active', 5) ON CONFLICT DO NOTHING""",
                [directive_id, cycle_id, rec[:300], severity]
            )
            directives_created += 1

        for issue in triage_audit.get("issues", [])[:3]:
            directive_id = f"dir-{uuid.uuid4().hex[:12]}"
            target_ticker = None
            tickers_list = issue.get("tickers", [])
            if tickers_list: target_ticker = tickers_list[0]
            severity = "warning" if issue["type"] in ("neglect", "over_glancing") else "info"
            db.execute(
                """INSERT INTO cycle_directives (id, cycle_id, directive_type, directive_text, target_ticker, severity, status, expires_after)
                VALUES (%s, %s, %s, %s, %s, %s, 'active', 3) ON CONFLICT DO NOTHING""",
                [directive_id, cycle_id, f"triage_{issue['type']}", issue["detail"][:300], target_ticker, severity]
            )
            directives_created += 1

        urgent_gaps = reflection.get("urgent_data_gaps", [])
        for ticker in urgent_gaps[:3]:
            directive_id = f"dir-{uuid.uuid4().hex[:12]}"
            db.execute(
                """INSERT INTO cycle_directives (id, cycle_id, directive_type, directive_text, target_ticker, severity, status, expires_after)
                VALUES (%s, %s, 'data_gap', %s, %s, 'warning', 'active', 3) ON CONFLICT DO NOTHING""",
                [directive_id, cycle_id, f"Critical data gap for {ticker}", ticker]
            )
            directives_created += 1

        sched_rec = reflection.get("schedule_recommendation")
        if sched_rec and isinstance(sched_rec, str) and len(sched_rec) >= 10:
            directive_id = f"dir-{uuid.uuid4().hex[:12]}"
            db.execute(
                """INSERT INTO cycle_directives (id, cycle_id, directive_type, directive_text, severity, status, expires_after)
                VALUES (%s, %s, 'schedule_recommendation', %s, 'info', 'active', 3) ON CONFLICT DO NOTHING""",
                [directive_id, cycle_id, sched_rec[:300]]
            )
            directives_created += 1

def _expire_old_directives() -> None:
    try:
        with get_db() as db:
            db.execute("UPDATE cycle_directives SET expires_after = expires_after - 1 WHERE status = 'active' AND expires_after > 0")
            db.execute("UPDATE cycle_directives SET status = 'expired', resolved_at = CURRENT_TIMESTAMP WHERE status = 'active' AND expires_after <= 0")
    except Exception as e:
        logger.debug("Directive expiry failed: %s", e)
