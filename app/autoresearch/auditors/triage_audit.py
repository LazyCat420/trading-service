import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def _audit_triage(cycle_id: str, cycle_summary: dict, tickers: list[str]) -> dict:
    result = {
        "glance_count": 0, "standard_count": 0, "deep_count": 0,
        "neglect_count": 0, "avg_consecutive_skips": 0.0,
        "stale_tickers": [], "issues": []
    }
    try:
        triage = cycle_summary.get("triage", {})
        result["glance_count"] = triage.get("glance", 0)
        result["standard_count"] = triage.get("standard", 0)
        result["deep_count"] = triage.get("deep", 0)

        from app.pipeline.attention_tracker import get_attention_summary, get_neglect_flags
        attention = get_attention_summary(tickers)
        neglect = get_neglect_flags()
        result["neglect_count"] = len(neglect)

        skip_counts = [a.consecutive_skips for a in attention.values()]
        if skip_counts:
            result["avg_consecutive_skips"] = round(sum(skip_counts) / len(skip_counts), 1)

        cutoff_48h = datetime.now(timezone.utc) - timedelta(hours=48)
        for ticker, attn in attention.items():
            if attn.last_analyzed_at is None or attn.last_analyzed_at < cutoff_48h:
                result["stale_tickers"].append(ticker)

        if result["neglect_count"] > 0:
            result["issues"].append({
                "type": "neglect", "detail": f"{result['neglect_count']} tickers flagged as neglected",
                "tickers": [n["ticker"] for n in neglect[:5]]
            })

        if result["avg_consecutive_skips"] > 3:
            result["issues"].append({
                "type": "over_glancing",
                "detail": f"Average {result['avg_consecutive_skips']} consecutive Glance skips"
            })

        total = result["glance_count"] + result["standard_count"] + result["deep_count"]
        if total > 0 and result["glance_count"] / total > 0.7:
            result["issues"].append({
                "type": "too_many_glance",
                "detail": f"{result['glance_count']}/{total} tickers in Glance tier"
            })
    except Exception as e:
        logger.debug("Triage audit failed: %s", e)
        result["issues"].append({"type": "audit_error", "detail": str(e)})

    return result
