import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.db.mongo_store import handle_mongo_read_failure
from app.db import mongo_query
from app.db import mongo_store

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
    rows = None
    try:
        from app.db import mongo_store
        docs = mongo_store.find_docs(
            "analysis_results", {"cycle_id": cycle_id},
            projection={"_id": 0, "ticker": 1, "result_json": 1},
        )
        rows = [(d.get("ticker"), d.get("result_json")) for d in docs]
    except Exception as me:
        logger.warning("[BattleRoyale] mongo results read failed: %s", me)
        rows = []


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

    # Regime + portfolio-math context. All of this already exists on the
    # cycle's whiteboard (regime_classification / quant_report sections) but
    # was never surfaced into the summary row, so the report couldn't answer
    # "what regime was this cycle traded in, and what did the math say?".
    regime_summary: dict[str, Any] = {}
    quant_risk: dict[str, Any] = {}
    try:
        from app.db import mongo_store
        wb_docs = mongo_store.find_docs(
            'whiteboard_entries',
            {'cycle_id': cycle_id, 'superseded_by': None, 'section': {'$in': ['regime_classification', 'quant_report']}},
        )
        for d in wb_docs:
            wb_ticker = d.get('ticker')
            wb_section = d.get('section')
            wb_content = d.get('content')
            try:
                content = wb_content if isinstance(wb_content, dict) else json.loads(wb_content)
            except Exception:
                continue
            if wb_section == "regime_classification":
                regime_summary[wb_ticker] = {
                    "regime": content.get("regime"),
                    "factors": content.get("factors"),
                }
            else:
                risk = content.get("risk_metrics") or {}
                quant_risk[wb_ticker] = {
                    "volatility_regime": risk.get("volatility_regime"),
                    "diversification_ratio": risk.get("diversification_ratio"),
                    "hrp_weight_suggestion": content.get("hrp_weight_suggestion"),
                }
        if regime_summary:
            regimes = {v.get("regime") for v in regime_summary.values() if v.get("regime")}
            report_content += (
                "\n#### Macro Regime & Portfolio Math\n"
                + f"Regime(s): {', '.join(sorted(regimes)) or 'n/a'}\n"
            )
            for tk in sorted(quant_risk):
                qr = quant_risk[tk]
                bits = [
                    f"vol={qr['volatility_regime']}" if qr.get("volatility_regime") else None,
                    f"div_ratio={qr['diversification_ratio']}" if qr.get("diversification_ratio") is not None else None,
                    f"hrp_suggest={qr['hrp_weight_suggestion']}" if qr.get("hrp_weight_suggestion") is not None else None,
                ]
                bits = [b for b in bits if b]
                if bits:
                    report_content += f"- **{tk}**: {', '.join(bits)}\n"
    except Exception as enrich_err:
        logger.warning("[BattleRoyale] Summary regime/quant enrichment skipped: %s", enrich_err)

    result_summary = json.dumps({
        "analyzed": len(tickers_data),
        "buy": len(buys),
        "sell": len(sells),
        "hold": len(holds),
        "unresolved": len(other),
        "top_buys": [{"ticker": b["ticker"], "confidence": b["confidence"]} for b in buys[:3]],
        "top_sells": [{"ticker": s["ticker"], "confidence": s["confidence"]} for s in sells[:3]],
        "regime": regime_summary,
        "quant_risk": quant_risk,
    })

    report_id = str(uuid.uuid4())
    _saved_at = datetime.now(timezone.utc)

    try:
        from app.db import mongo_store
        _summary = result_summary
        if isinstance(_summary, str):
            try:
                _summary = json.loads(_summary)
            except (ValueError, TypeError):
                pass
        mongo_store.upsert_doc("ticker_reports", {"cycle_id": cycle_id, "is_summary": True}, {
            "id": report_id, "cycle_id": cycle_id, "ticker": "GLOBAL", "action": "HOLD",
            "confidence": 0, "report_markdown": report_content, "result_summary": _summary,
            "is_summary": True, "created_at": _saved_at,
        })
        logger.info("[BattleRoyale] Report saved with ID %s", report_id)
        return True
    except Exception as e:
        logger.error("[BattleRoyale] Failed to save report for %s: %s", cycle_id, e)
        _record_report_failure(cycle_id, str(e))
        return False


def _record_report_failure(cycle_id: str, detail: str) -> None:
    """Best-effort terminal event so a failed report write is observable."""
    try:
        _evt = {
            "id": str(uuid.uuid4()),
            "cycle_id": cycle_id,
            "timestamp": datetime.now(timezone.utc),
            "phase": "reporting",
            "step": "battle_royale_save_failed",
            "detail": f"Cycle summary report failed to persist: {detail[:300]}",
            "status": "error",
        }
        from app.db import mongo_store
        mongo_store.insert_docs('pipeline_events', [_evt])
    except Exception as ev_err:  # pragma: no cover - diagnostics only
        logger.error("[BattleRoyale] Could not record report-failure event for %s: %s", cycle_id, ev_err)
