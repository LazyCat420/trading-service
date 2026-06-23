import json
import logging
from datetime import datetime, timezone
from app.db.connection import get_db

logger = logging.getLogger(__name__)

def save_analysis_result(ticker: str, cycle_id: str, result: dict):
    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO analysis_results (
                    ticker, cycle_id, result_json, confidence,
                    thesis_verdict, thesis_confidence, thesis_summary,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, cycle_id) DO UPDATE SET
                    result_json = EXCLUDED.result_json,
                    confidence = EXCLUDED.confidence,
                    thesis_verdict = EXCLUDED.thesis_verdict,
                    thesis_confidence = EXCLUDED.thesis_confidence,
                    thesis_summary = EXCLUDED.thesis_summary,
                    updated_at = EXCLUDED.updated_at
                """,
                [
                    ticker,
                    cycle_id,
                    json.dumps(result),
                    result.get("confidence", 0),
                    result.get("action", "HOLD"),
                    result.get("confidence", 0),
                    result.get("rationale", ""),
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                ]
            )
        logger.info("[result_saver] Saved analysis result for %s in cycle %s", ticker, cycle_id)
    except Exception as e:
        logger.error("[result_saver] Failed to save result for %s: %s", ticker, e)

