import logging
from typing import Any
from app.db.connection import get_db

logger = logging.getLogger(__name__)

async def run_battle_royale(cycle_id: str, bot_id: str):
    """
    Stage 1: Sector-Based Battle Royale
    Stage 2: Cross-Sector Allocation
    Saves the final report into `research_reports` or `swarm_verdicts` so the UI picks it up.
    """
    logger.info("[BattleRoyale] Starting Battle Royale for cycle %s", cycle_id)
    
    # 1. Gather all analysis results for this cycle
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker, result_json FROM analysis_results WHERE cycle_id = %s",
            [cycle_id]
        ).fetchall()
        
    if not rows:
        logger.warning("[BattleRoyale] No analysis results found for cycle %s", cycle_id)
        return
        
    # Build summary of tickers
    tickers_data = []
    import json
    for ticker, result_str in rows:
        try:
            result = json.loads(result_str)
            action = result.get("action", "HOLD")
            confidence = result.get("confidence", 0)
            rationale = result.get("rationale", "")
            tickers_data.append({"ticker": ticker, "action": action, "confidence": confidence, "rationale": rationale})
        except Exception:
            pass
            
    buys = [t for t in tickers_data if t["action"] == "BUY"]
    sells = [t for t in tickers_data if t["action"] == "SELL"]
    
    # Sort by confidence
    buys = sorted(buys, key=lambda x: x["confidence"], reverse=True)
    sells = sorted(sells, key=lambda x: x["confidence"], reverse=True)
    
    report_content = f"### Battle Royale Summary (Cycle: {cycle_id})\n\n"
    report_content += "#### Top Buys\n"
    for b in buys[:3]:
        report_content += f"- **{b['ticker']}** (Confidence: {b['confidence']}%): {b['rationale'][:100]}...\n"
        
    report_content += "\n#### Top Sells\n"
    for s in sells[:3]:
        report_content += f"- **{s['ticker']}** (Confidence: {s['confidence']}%): {s['rationale'][:100]}...\n"
        
    if not buys and not sells:
        report_content += "No actionable signals generated in this cycle.\n"
        
    # Save to research_reports
    import uuid
    from datetime import datetime, timezone
    report_id = str(uuid.uuid4())
    
    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO ticker_reports (id, cycle_id, ticker, action, confidence, report_markdown, result_summary, is_summary, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    report_id,
                    cycle_id,
                    "GLOBAL",
                    "HOLD",
                    0,
                    report_content,
                    '{}',
                    True,
                    datetime.now(timezone.utc)
                ]
            )
        logger.info("[BattleRoyale] Report saved with ID %s", report_id)
    except Exception as e:
        logger.error("[BattleRoyale] Failed to save report: %s", e)

