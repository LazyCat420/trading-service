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
        # Best-effort Mongo mirror — one analysis_results doc per (cycle_id, ticker);
        # result_json stored as a native dict.
        try:
            from app.db import mongo_store
            if mongo_store.writes_mongo("analysis_results"):
                mongo_store.upsert_doc("analysis_results", {"cycle_id": cycle_id, "ticker": ticker}, {
                    "id": result_id, "ticker": ticker, "cycle_id": cycle_id,
                    "bot_id": result.get("bot_id", "cycle-backend"), "result_json": result,
                    "confidence": result.get("confidence", 0), "thesis_verdict": result.get("action", "HOLD"),
                    "thesis_confidence": result.get("confidence", 0), "thesis_summary": result.get("rationale", ""),
                    "created_at": datetime.now(timezone.utc), "triage_tier": result.get("triage_tier", "standard"),
                    "analysis_price": analysis_price, "analysis_rsi": analysis_rsi,
                    "analysis_fund_count": analysis_fund_count,
                })
        except Exception as me:
            logger.warning("[result_saver] Mongo mirror failed (non-fatal): %s", me)
        logger.info("[result_saver] Saved analysis result for %s in cycle %s (price=%.2f, rsi=%.1f, funds=%d)",
                     ticker, cycle_id,
                     analysis_price or 0, analysis_rsi or 0, analysis_fund_count or 0)
    except Exception as e:
        # A silently-swallowed analysis write means the ticker vanishes from the
        # reports UI with no trace. Keep it non-fatal (do not abort the cycle),
        # but make the failure observable in the cycle's event stream.
        logger.error("[result_saver] Failed to save result for %s: %s", ticker, e)
        try:
            # pipeline_events.id is TEXT PRIMARY KEY with no default — supply it
            # explicitly (same as append_events). Build once so the PG row and
            # the Mongo mirror share an id.
            _evt = {
                "id": str(uuid.uuid4()),
                "cycle_id": cycle_id,
                "timestamp": datetime.now(timezone.utc),
                "phase": "reporting",
                "step": f"analysis_save_failed_{ticker}",
                "detail": f"Analysis result for {ticker} failed to persist: {str(e)[:300]}",
                "status": "error",
            }
            with get_db() as db:
                db.execute(
                    """
                    INSERT INTO pipeline_events (id, cycle_id, timestamp, phase, step, detail, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [_evt["id"], _evt["cycle_id"], _evt["timestamp"], _evt["phase"],
                     _evt["step"], _evt["detail"], _evt["status"]],
                )
            from app.db import mongo_store
            mongo_store.mirror_pipeline_event(_evt)
        except Exception as ev_err:
            logger.error("[result_saver] Could not record save-failure event for %s: %s", ticker, ev_err)


