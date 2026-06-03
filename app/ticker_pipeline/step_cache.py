"""
Step Cache — Fast-tracks tickers that already have recent, high-confidence reports.

If a ticker was analyzed successfully within the last N hours, we can skip
the expensive LLM processing and reuse the recent result. This prevents the
queue from saturating when reprocessing the same watchlist.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from app.ticker_pipeline.context import TickerContext
from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Skip tickers with reports less than 6 hours old
CACHE_WINDOW_HOURS = 6
# Only skip if the previous report was confident
MIN_CONFIDENCE = 60


async def run_cache_step(ctx: TickerContext) -> dict[str, Any] | None:
    """Check if the ticker has a recent report. If so, return it."""
    
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=CACHE_WINDOW_HOURS)
    
    try:
        with get_db() as db:
            row = db.execute(
                """
                SELECT result_json, created_at, confidence 
                FROM analysis_results 
                WHERE ticker = %s 
                  AND created_at >= %s
                  AND confidence >= %s
                  AND result_json IS NOT NULL
                ORDER BY created_at DESC 
                LIMIT 1
                """,
                [ctx.ticker, cutoff_time, MIN_CONFIDENCE]
            ).fetchone()
            
            if row:
                import json
                result_json_str = row[0]
                created_at = row[1]
                confidence = row[2]
                
                try:
                    result = json.loads(result_json_str)
                    
                    # Ensure it has the base required fields
                    if "action" in result and "confidence" in result:
                        logger.info(
                            "[CACHE] %s has a recent report (conf=%d, age=%s). Skipping LLM analysis.", 
                            ctx.ticker, confidence, created_at
                        )
                        ctx.safe_emit(
                            "analyzing", f"cache_hit_{ctx.ticker}",
                            f"⚡ {ctx.ticker}: Reusing cached report from {created_at.strftime('%H:%M')} (Conf: {confidence}%)",
                            status="ok",
                        )
                        
                        # Add a flag to show this was a cached result
                        result["is_cached"] = True
                        result["cached_from"] = created_at.isoformat()
                        
                        return result
                except json.JSONDecodeError:
                    logger.warning("[CACHE] Failed to parse result_json for %s", ctx.ticker)
                    
    except Exception as e:
        logger.warning("[CACHE] DB query failed for %s: %s", ctx.ticker, e)

    return None
