import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def _audit_schedule_health() -> dict:
    result = {
        "active_count": 0, "total_count": 0, "avg_interval_hours": None,
        "has_premarket": False, "stuck_schedules": [], "issues": []
    }
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT id, name, schedule_type, cron_expression, interval_hours, is_active, last_run_at, next_run_at FROM cycle_schedules ORDER BY is_active DESC"
            ).fetchall()

        result["total_count"] = len(rows)
        active_rows = [r for r in rows if r[5]]
        result["active_count"] = len(active_rows)

        if result["active_count"] == 0:
            result["issues"].append({
                "type": "no_active_schedules", "severity": "critical",
                "detail": "Bot has NO active schedules."
            })
            return result

        intervals = [r[4] for r in active_rows if r[2] == "interval" and r[4] is not None and r[4] > 0]
        if intervals:
            result["avg_interval_hours"] = round(sum(intervals) / len(intervals), 1)

        for r in active_rows:
            if r[2] == "cron" and r[3]:
                cron_parts = r[3].split()
                if len(cron_parts) >= 2:
                    try:
                        hour = int(cron_parts[1])
                        if 7 <= hour <= 9: result["has_premarket"] = True
                    except ValueError: pass

        now = datetime.now(timezone.utc)
        for r in active_rows:
            if r[2] == "interval" and r[4] and r[6]:
                last_run = r[6]
                if hasattr(last_run, "timestamp"):
                    expected_gap = r[4] * 3600
                    actual_gap = (now - last_run).total_seconds()
                    if actual_gap > expected_gap * 2.5:
                        result["stuck_schedules"].append({
                            "id": r[0], "name": r[1], "expected_interval_h": r[4],
                            "actual_gap_h": round(actual_gap / 3600, 1)
                        })

        if result["stuck_schedules"]:
            result["issues"].append({
                "type": "stuck_schedules", "severity": "warning",
                "detail": f"{len(result['stuck_schedules'])} schedule(s) appear stuck",
                "schedules": [s["name"] for s in result["stuck_schedules"]]
            })

        if not result["has_premarket"] and result["active_count"] > 0:
            result["issues"].append({
                "type": "no_premarket", "severity": "info",
                "detail": "No pre-market (7-9 AM ET) schedule found."
            })
    except Exception as e:
        logger.debug("Schedule health audit failed: %s", e)
        result["issues"].append({"type": "audit_error", "detail": str(e)})

    return result
