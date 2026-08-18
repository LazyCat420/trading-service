import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db import mongo_query

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
        tier_rows = mongo_query.group_rows(
            'analysis_results', {'cycle_id': cycle_id}, ['triage_tier'],
            [('count', None)], [('key', 'triage_tier'), ('agg', 0)],
        )
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
            last_rows = mongo_query.group_rows(
                'analysis_results', {'ticker': {'$in': list(tickers)}},
                ['ticker'], [('max', 'created_at')],
                [('key', 'ticker'), ('agg', 0)],
            )
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
