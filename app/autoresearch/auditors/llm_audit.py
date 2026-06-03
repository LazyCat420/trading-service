import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def _audit_llm_traces(cycle_id: str) -> dict:
    issues = []
    try:
        from app.monitoring.llm_tracker import tracker
        stats = tracker.get_stats()
        total_calls = stats.get("total_calls", 0)
        failed = stats.get("failed_calls", 0)
        if total_calls == 0:
            return {"score": 0.5, "issues": []}
        fail_rate = failed / total_calls
        if fail_rate > 0.1:
            issues.append({"issue": f"LLM failure rate: {fail_rate:.0%}", "severity": "warning"})
        score = max(0, 1.0 - fail_rate * 2)
        return {
            "score": round(score, 3),
            "total_calls": total_calls,
            "failed_calls": failed,
            "fail_rate": round(fail_rate, 3),
            "issues": issues,
        }
    except Exception:
        return {"score": 0.5, "issues": []}
