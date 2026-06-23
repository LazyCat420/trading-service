import json
import logging
import uuid
from datetime import datetime, timezone
from app.db.connection import get_db

logger = logging.getLogger(__name__)

def save_analysis_result(ticker: str, cycle_id: str, result: dict):
    try:
        with get_db() as db:
            with db.transaction():
                # Delete existing record for this ticker and cycle to avoid duplicates
                db.execute(
                    "DELETE FROM analysis_results WHERE ticker = %s AND cycle_id = %s",
                    [ticker, cycle_id]
                )
                
                result_id = str(uuid.uuid4())
                db.execute(
                    """
                    INSERT INTO analysis_results (
                        id, ticker, cycle_id, bot_id, result_json, confidence,
                        thesis_verdict, thesis_confidence, thesis_summary,
                        created_at, triage_tier
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        result.get("triage_tier", "standard")
                    ]
                )
        logger.info("[result_saver] Saved analysis result for %s in cycle %s", ticker, cycle_id)
    except Exception as e:
        logger.error("[result_saver] Failed to save result for %s: %s", ticker, e)


