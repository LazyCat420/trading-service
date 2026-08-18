import json
import logging
import uuid
from datetime import datetime, timezone
from app.db import mongo_store

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
        _saved_at = datetime.now(timezone.utc)
        result_id = str(uuid.uuid4())
        analysis_price = None
        analysis_rsi = None
        analysis_fund_count = 0
        if snapshot:
            analysis_price = snapshot.get("price")
            analysis_rsi = snapshot.get("rsi")
            analysis_fund_count = snapshot.get("fund_count", 0)

        mongo_store.upsert_doc("analysis_results", {"cycle_id": cycle_id, "ticker": ticker}, {
            "id": result_id, "ticker": ticker, "cycle_id": cycle_id,
            "bot_id": result.get("bot_id", "cycle-backend"), "result_json": result,
            "confidence": result.get("confidence", 0), "thesis_verdict": result.get("action", "HOLD"),
            "thesis_confidence": result.get("confidence", 0), "thesis_summary": result.get("rationale", ""),
            "thesis_unchanged": False,
            "created_at": _saved_at, "triage_tier": result.get("triage_tier", "standard"),
            "analysis_price": analysis_price, "analysis_rsi": analysis_rsi,
            "analysis_fund_count": analysis_fund_count,
        })
        logger.info("[result_saver] Saved analysis result for %s in cycle %s (price=%.2f, rsi=%.1f, funds=%d)",
                     ticker, cycle_id,
                     analysis_price or 0, analysis_rsi or 0, analysis_fund_count or 0)
    except Exception as e:
        logger.error("[result_saver] Failed to save result for %s: %s", ticker, e)
        try:
            _evt = {
                "id": str(uuid.uuid4()),
                "cycle_id": cycle_id,
                "timestamp": datetime.now(timezone.utc),
                "phase": "reporting",
                "step": f"analysis_save_failed_{ticker}",
                "detail": f"Analysis result for {ticker} failed to persist: {str(e)[:300]}",
                "status": "error",
            }
            mongo_store.insert_docs('pipeline_events', [_evt])
        except Exception as ev_err:
            logger.error("[result_saver] Could not record save-failure event for %s: %s", ticker, ev_err)


