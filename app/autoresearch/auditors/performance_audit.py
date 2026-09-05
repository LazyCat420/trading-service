import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db import mongo_query
from app.db import mongo_store

logger = logging.getLogger(__name__)


def _audit_performance(cycle_id: str, cycle_summary: dict) -> dict:
    return {
        "total_ms": cycle_summary.get("elapsed_ms", 0),
        "tickers_analyzed": cycle_summary.get("analysis_results_count", 0),
        "collector_ok": cycle_summary.get("collector_ok", 0),
        "collector_skipped": cycle_summary.get("collector_skipped", 0),
        "collector_error": cycle_summary.get("collector_error", 0),
        "collector_late": cycle_summary.get("collector_late", 0),
        "collector_late_names": cycle_summary.get("collector_late_names", []),
        "collector_failures": cycle_summary.get("collector_failures", []),
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
        from app.db import mongo_store
        if mongo_store.reads_mongo("execution_errors"):
            try:
                docs = mongo_store.find_docs(
                    "execution_errors",
                    {"cycle_id": cycle_id},
                    sort=[("created_at", -1)],
                    limit=5,
                    projection={"phase": 1, "error_type": 1, "error_message": 1},
                )
                return [{"phase": d.get("phase"), "error_type": d.get("error_type"), "error_message": d.get("error_message")} for d in docs]
            except Exception as me:
                mongo_store.handle_mongo_read_failure("execution_errors", "_audit_execution_errors", me)

        rows = mongo_query.find_rows('execution_errors', {'cycle_id': cycle_id}, ['phase', 'error_type', 'error_message'], sort=[('created_at', -1)], limit=5)
        return [{"phase": r[0], "error_type": r[1], "error_message": r[2]} for r in rows]
    except Exception as e:
        logger.debug("Failed to fetch execution errors: %s", e)
    return []
