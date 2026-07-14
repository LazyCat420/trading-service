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
    """Audit triage-tier distribution and ticker staleness.

    Counts come from analysis_results.triage_tier for this cycle (the old
    app.pipeline.attention_tracker source was deleted in the V3 purge, which
    made this audit silently error out every cycle).
    """
    result = {
        "glance_count": 0, "standard_count": 0, "deep_count": 0,
        "neglect_count": 0, "avg_consecutive_skips": 0.0,
        "stale_tickers": [], "issues": []
    }
    try:
        with get_db() as db:
            tier_rows = db.execute(
                "SELECT triage_tier, COUNT(*) FROM analysis_results "
                "WHERE cycle_id = %s GROUP BY triage_tier",
                [cycle_id],
            ).fetchall()
        for tier, count in tier_rows:
            tier = (tier or "").lower()
            if "glance" in tier:
                result["glance_count"] += count
            elif "deep" in tier or "full" in tier:
                result["deep_count"] += count
            else:
                result["standard_count"] += count

        # Stale = analyzed tickers whose latest analysis is older than 48h
        if tickers:
            cutoff_48h = datetime.now(timezone.utc) - timedelta(hours=48)
            placeholders = ",".join(["%s"] * len(tickers))
            with get_db() as db:
                last_rows = db.execute(
                    f"SELECT ticker, MAX(created_at) FROM analysis_results "
                    f"WHERE ticker IN ({placeholders}) GROUP BY ticker",
                    list(tickers),
                ).fetchall()
            last_map = {r[0]: r[1] for r in last_rows}
            for ticker in tickers:
                last = last_map.get(ticker)
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if last is None or last < cutoff_48h:
                    result["stale_tickers"].append(ticker)

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
