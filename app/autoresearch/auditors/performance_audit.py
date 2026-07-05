import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def _audit_performance(cycle_id: str, cycle_summary: dict) -> dict:
    return {
        "total_ms": cycle_summary.get("elapsed_ms", 0),
        "tickers_analyzed": cycle_summary.get("analysis_results_count", 0),
        "collector_ok": cycle_summary.get("collector_ok", 0),
        "collector_skipped": cycle_summary.get("collector_skipped", 0),
        "collector_error": cycle_summary.get("collector_error", 0),
        "trade_executed": cycle_summary.get("trade_executed", 0),
        "status": cycle_summary.get("status", "unknown"),
    }

def _audit_recovery() -> dict:
    try:
        from app.recovery.engine import recovery_engine
        return {
            **recovery_engine.get_stats(),
            "recent_events": recovery_engine.get_history(10),
        }
    except Exception:
        return {"total_failures": 0, "by_type": {}, "circuit_breakers_tripped": 0}

def _audit_execution_errors(cycle_id: str) -> list[dict]:
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT phase, error_type, error_message FROM execution_errors WHERE cycle_id = %s ORDER BY created_at DESC LIMIT 5",
                (cycle_id,),
            ).fetchall()
            return [{"phase": r[0], "error_type": r[1], "error_message": r[2]} for r in rows]
    except Exception as e:
        logger.debug("Failed to fetch execution errors: %s", e)
    return []
