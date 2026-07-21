import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.db.connection import get_db

logger = logging.getLogger(__name__)

async def run_battle_royale(cycle_id: str, bot_id: str) -> bool:
    # Returns True when the cycle report row was written (feeds
    # cycle_run_summaries.report_generated, which was never set before).
    """
    Stage 1: Sector-Based Battle Royale
    Stage 2: Cross-Sector Allocation
    Saves the final report into `ticker_reports` so the UI picks it up.
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
        return False
        
    # Build summary of tickers
    tickers_data = []
    for ticker, result_raw in rows:
        try:
            # Handle both JSONB (already dict) and TEXT (string) column types
            if isinstance(result_raw, str):
                result = json.loads(result_raw)
            else:
                result = result_raw
            action = result.get("action", "HOLD")
            confidence = result.get("confidence", 0)
            rationale = result.get("rationale", "")
            tickers_data.append({"ticker": ticker, "action": action, "confidence": confidence, "rationale": rationale})
        except Exception:
            pass
            
    def _by_conf(items):
        return sorted(items, key=lambda x: x["confidence"], reverse=True)

    buys = _by_conf([t for t in tickers_data if t["action"] == "BUY"])
    sells = _by_conf([t for t in tickers_data if t["action"] == "SELL"])
    holds = _by_conf([t for t in tickers_data if t["action"] == "HOLD"])
    # Anything not BUY/SELL/HOLD (blank/errored decision) — surface it rather
    # than silently dropping, so an all-error cycle does not read as "no signal".
    other = [t for t in tickers_data if t["action"] not in ("BUY", "SELL", "HOLD")]

    report_content = f"### Battle Royale Summary (Cycle: {cycle_id})\n\n"
    report_content += (
        f"Analyzed **{len(tickers_data)}** tickers — "
        f"{len(buys)} buy, {len(sells)} sell, {len(holds)} hold"
        + (f", {len(other)} unresolved" if other else "")
        + ".\n\n"
    )

    report_content += "#### Top Buys\n"
    report_content += "".join(
        f"- **{b['ticker']}** (Confidence: {b['confidence']}%): {b['rationale'][:100]}...\n"
        for b in buys[:3]
    ) or "_None._\n"

    report_content += "\n#### Top Sells\n"
    report_content += "".join(
        f"- **{s['ticker']}** (Confidence: {s['confidence']}%): {s['rationale'][:100]}...\n"
        for s in sells[:3]
    ) or "_None._\n"

    # HOLD context: previously omitted entirely, which made an all-HOLD cycle
    # render as an empty report. List the highest-conviction holds so the
    # summary reflects that the cycle DID produce decisions.
    if holds:
        report_content += "\n#### Notable Holds\n"
        report_content += "".join(
            f"- **{h['ticker']}** (Confidence: {h['confidence']}%): {h['rationale'][:100]}...\n"
            for h in holds[:3]
        )

    if other:
        report_content += "\n#### Unresolved\n"
        report_content += "".join(
            f"- **{o['ticker']}**: {(o['rationale'] or 'no decision produced')[:100]}\n"
            for o in other[:5]
        )

    if not tickers_data:
        report_content += "No analysis results were recorded for this cycle.\n"

    # Structured counterpart of the markdown (was hardcoded '{}'). Consumers may
    # ignore it today, but recording it stops the field being permanently dead.
    result_summary = json.dumps({
        "analyzed": len(tickers_data),
        "buy": len(buys),
        "sell": len(sells),
        "hold": len(holds),
        "unresolved": len(other),
        "top_buys": [{"ticker": b["ticker"], "confidence": b["confidence"]} for b in buys[:3]],
        "top_sells": [{"ticker": s["ticker"], "confidence": s["confidence"]} for s in sells[:3]],
    })

    # Save to ticker_reports
    report_id = str(uuid.uuid4())

    try:
        with get_db() as db:
            # Idempotent per cycle: a re-run of the same cycle_id replaces the
            # prior summary instead of leaving duplicate is_summary rows for the
            # reader's ORDER BY created_at DESC to disambiguate.
            db.execute(
                "DELETE FROM ticker_reports WHERE cycle_id = %s AND is_summary = TRUE",
                [cycle_id],
            )
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
                    result_summary,
                    True,
                    datetime.now(timezone.utc)
                ]
            )
        logger.info("[BattleRoyale] Report saved with ID %s", report_id)
        return True
    except Exception as e:
        # Fail loud: a swallowed report-write is exactly why cycle summaries
        # went blank unnoticed. Surface it in the cycle's own event stream (the
        # reports UI reads /reports/cycle/{id}/events) so the failure is visible,
        # then still return False so the caller's report_generated flag is honest.
        logger.error("[BattleRoyale] Failed to save report for %s: %s", cycle_id, e)
        _record_report_failure(cycle_id, str(e))
        return False


def _record_report_failure(cycle_id: str, detail: str) -> None:
    """Best-effort terminal event so a failed report write is observable.

    Wrapped in its own guard: if even this write fails we only log — surfacing
    the failure must never itself break the cycle.
    """
    try:
        with get_db() as db:
            # pipeline_events.id is TEXT PRIMARY KEY with no default — it must
            # be supplied explicitly (same as PipelineStateDB.append_events).
            db.execute(
                """
                INSERT INTO pipeline_events (id, cycle_id, timestamp, phase, step, detail, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    str(uuid.uuid4()),
                    cycle_id,
                    datetime.now(timezone.utc),
                    "reporting",
                    "battle_royale_save_failed",
                    f"Cycle summary report failed to persist: {detail[:300]}",
                    "error",
                ],
            )
    except Exception as ev_err:  # pragma: no cover - diagnostics only
        logger.error("[BattleRoyale] Could not record report-failure event for %s: %s", cycle_id, ev_err)
