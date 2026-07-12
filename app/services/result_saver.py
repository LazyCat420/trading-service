import json
import logging
import uuid
from datetime import datetime, timezone
from app.db.connection import get_db

logger = logging.getLogger(__name__)

def save_analysis_result(ticker: str, cycle_id: str, result: dict, snapshot: dict | None = None):
    """Save analysis result with optional market snapshot for the Freshness Gate.

    Args:
        ticker: Stock ticker symbol.
        cycle_id: Pipeline cycle ID.
        result: Analysis result dict (action, confidence, rationale, etc.).
        snapshot: Optional dict with {price, rsi, fund_count} at analysis time.
            Used by the Freshness Gate to compute deltas on the next cycle.
    """
    try:
        with get_db() as db:
            with db.transaction():
                # Delete existing record for this ticker and cycle to avoid duplicates
                db.execute(
                    "DELETE FROM analysis_results WHERE ticker = %s AND cycle_id = %s",
                    [ticker, cycle_id]
                )
                
                result_id = str(uuid.uuid4())
                # Extract snapshot values (Freshness Gate baseline)
                analysis_price = None
                analysis_rsi = None
                analysis_fund_count = 0
                if snapshot:
                    analysis_price = snapshot.get("price")
                    analysis_rsi = snapshot.get("rsi")
                    analysis_fund_count = snapshot.get("fund_count", 0)

                db.execute(
                    """
                    INSERT INTO analysis_results (
                        id, ticker, cycle_id, bot_id, result_json, confidence,
                        thesis_verdict, thesis_confidence, thesis_summary,
                        created_at, triage_tier,
                        analysis_price, analysis_rsi, analysis_fund_count
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        result_id,
                        ticker,
                        cycle_id,
                        result.get("bot_id", "cycle-backend"),
                        json.dumps(result),
                        result.get("confidence", 0),
                        result.get("action", "HOLD"),
                        result.get("confidence", 0),
                        result.get("rationale", ""),
                        datetime.now(timezone.utc),
                        result.get("triage_tier", "standard"),
                        analysis_price,
                        analysis_rsi,
                        analysis_fund_count,
                    ]
                )
        logger.info("[result_saver] Saved analysis result for %s in cycle %s (price=%.2f, rsi=%.1f, funds=%d)",
                     ticker, cycle_id,
                     analysis_price or 0, analysis_rsi or 0, analysis_fund_count or 0)
    except Exception as e:
        logger.error("[result_saver] Failed to save result for %s: %s", ticker, e)


