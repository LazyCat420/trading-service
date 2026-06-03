import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def _audit_decisions(cycle_id: str, cycle_summary: dict) -> dict:
    buy = cycle_summary.get("buy_count", 0)
    sell = cycle_summary.get("sell_count", 0)
    hold = cycle_summary.get("hold_count", 0)
    total = buy + sell + hold
    issues = []
    outcome_stats = {}

    if total == 0:
        return {"score": 0, "issues": [{"issue": "No decisions produced", "severity": "critical"}]}

    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT confidence FROM analysis_results WHERE cycle_id=%s AND confidence IS NOT NULL",
                [cycle_id],
            ).fetchall()
            if rows:
                confs = [r[0] for r in rows]
                if max(confs) - min(confs) < 10 and len(confs) >= 3:
                    issues.append({"issue": f"Uniform confidence ({min(confs)}-{max(confs)})", "severity": "info"})
                if sum(confs) / len(confs) < 40:
                    issues.append({"issue": f"Low avg confidence: {sum(confs) / len(confs):.0f}%", "severity": "warning"})
    except Exception:
        pass

    try:
        with get_db() as db:
            resolved = db.execute(
                """
                SELECT action, confidence, pnl_pct, outcome
                FROM decision_outcomes
                WHERE resolved_at IS NOT NULL AND outcome != 'CANCELED' AND resolved_at > CURRENT_TIMESTAMP - INTERVAL '30 days'
                ORDER BY resolved_at DESC LIMIT 100
                """,
            ).fetchall()

            if len(resolved) >= 3:
                wins = [r for r in resolved if r[3] == "WIN"]
                losses = [r for r in resolved if r[3] == "LOSS"]
                flats = [r for r in resolved if r[3] == "FLAT"]

                win_rate = len(wins) / len(resolved)
                avg_win_pnl = sum(r[2] for r in wins) / len(wins) if wins else 0
                avg_loss_pnl = sum(abs(r[2]) for r in losses) / len(losses) if losses else 0

                win_rate_score = min(1.0, win_rate)
                high_conf = [r for r in resolved if r[1] >= 70]
                low_conf = [r for r in resolved if r[1] < 50]

                if high_conf and low_conf:
                    high_win_rate = len([r for r in high_conf if r[3] == "WIN"]) / len(high_conf)
                    low_win_rate = len([r for r in low_conf if r[3] == "WIN"]) / len(low_conf)
                    calibration_gap = high_win_rate - low_win_rate
                    calibration_score = min(1.0, max(0.0, 0.5 + calibration_gap))
                elif high_conf:
                    calibration_score = len([r for r in high_conf if r[3] == "WIN"]) / len(high_conf)
                else:
                    calibration_score = 0.5

                if avg_loss_pnl > 0:
                    profit_factor = avg_win_pnl / avg_loss_pnl
                    risk_score = min(1.0, profit_factor / 2.0)
                elif avg_win_pnl > 0:
                    risk_score = 1.0
                else:
                    risk_score = 0.3

                score = (win_rate_score * 0.4 + calibration_score * 0.3 + risk_score * 0.3)

                outcome_stats = {
                    "total_resolved": len(resolved),
                    "wins": len(wins),
                    "losses": len(losses),
                    "flats": len(flats),
                    "win_rate": round(win_rate, 3),
                    "avg_win_pnl": round(avg_win_pnl, 2),
                    "avg_loss_pnl": round(avg_loss_pnl, 2),
                    "calibration_score": round(calibration_score, 3),
                    "risk_score": round(risk_score, 3),
                    "scoring_method": "outcome_based",
                }

                if win_rate < 0.40:
                    issues.append({"issue": f"Low win rate: {win_rate:.0%} ({len(wins)}/{len(resolved)})", "severity": "critical"})
                if avg_loss_pnl > 0 and avg_win_pnl < avg_loss_pnl:
                    issues.append({"issue": f"Avg loss ({avg_loss_pnl:.1f}%) > avg win ({avg_win_pnl:.1f}%)", "severity": "warning"})
                if calibration_score < 0.35:
                    issues.append({"issue": "Conviction miscalibrated", "severity": "warning"})

                try:
                    _backfill_cycle_summaries(db)
                except Exception as bf_err:
                    logger.debug("Summaries backfill failed (non-fatal): %s", bf_err)
            else:
                score = 0.5
                outcome_stats = {
                    "total_resolved": len(resolved),
                    "scoring_method": "cold_start",
                    "note": f"Need >= 3 resolved, have {len(resolved)}",
                }
                if buy + sell == 0 and total >= 3:
                    issues.append({"issue": "Zero BUY/SELL signals (cold start)", "severity": "info"})
                    score = 0.4
    except Exception as outcome_err:
        logger.warning("[AUTORESEARCH] Outcome-based scoring failed: %s", outcome_err)
        score = 0.5
        outcome_stats = {"scoring_method": "fallback_error", "error": str(outcome_err)}

    critical_issues = [i for i in issues if i.get("severity") == "critical"]
    if critical_issues:
        score *= max(0.5, 1.0 - len(critical_issues) * 0.2)

    return {
        "score": round(score, 3),
        "buy": buy,
        "sell": sell,
        "hold": hold,
        "issues": issues,
        "outcome_stats": outcome_stats,
    }

def _backfill_cycle_summaries(db) -> None:
    db.execute(
        """
        UPDATE cycle_summaries cs
        SET was_correct = CASE
                WHEN do.outcome = 'WIN' THEN TRUE
                WHEN do.outcome = 'LOSS' THEN FALSE
                ELSE NULL
            END,
            outcome_pnl = do.pnl_pct
        FROM decision_outcomes do
        WHERE cs.ticker = do.ticker AND cs.action = do.action
          AND do.resolved_at IS NOT NULL AND do.outcome != 'CANCELED' AND cs.was_correct IS NULL
          AND cs.cycle_date >= do.created_at - INTERVAL '1 day' AND cs.cycle_date <= do.created_at + INTERVAL '1 day'
        """
    )

def write_cycle_summary(cycle_id: str, analysis_results: list[dict]) -> None:
    if not analysis_results: return
    try:
        buy_count = sum(1 for r in analysis_results if r.get("action") == "BUY")
        sell_count = sum(1 for r in analysis_results if r.get("action") == "SELL")
        hold_count = sum(1 for r in analysis_results if r.get("action") == "HOLD")

        confidences = [r.get("confidence", 0) or 0 for r in analysis_results]
        avg_conf = sum(confidences) / len(confidences) if confidences else 0

        top = max(analysis_results, key=lambda r: r.get("confidence", 0) or 0)
        top_confidence = top.get("confidence", 0) or 0
        top_ticker = top.get("ticker", "?") if top_confidence > 0 else None

        top_desc = f"Top pick: {top_ticker} @ {top_confidence}%." if top_ticker else "No high-confidence picks."
        lesson = f"{buy_count} BUY / {sell_count} SELL / {hold_count} HOLD. {top_desc}"

        with get_db() as db:
            db.execute(
                """INSERT INTO autoresearch_cycle_summaries
                (id, cycle_id, total_tickers, buy_count, sell_count, hold_count, avg_confidence, top_ticker, top_confidence, lesson_summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cycle_id) DO UPDATE SET
                    total_tickers = EXCLUDED.total_tickers, buy_count = EXCLUDED.buy_count, sell_count = EXCLUDED.sell_count,
                    hold_count = EXCLUDED.hold_count, avg_confidence = EXCLUDED.avg_confidence, top_ticker = EXCLUDED.top_ticker,
                    top_confidence = EXCLUDED.top_confidence, lesson_summary = EXCLUDED.lesson_summary""",
                (
                    f"cs-{uuid.uuid4().hex[:12]}", cycle_id, len(analysis_results), buy_count, sell_count, hold_count,
                    round(avg_conf, 1), top_ticker, top_confidence, lesson[:500]
                )
            )
    except Exception as e:
        logger.warning("cycle_summaries write failed (non-fatal): %s", e)
