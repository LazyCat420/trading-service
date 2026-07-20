import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Version stamp for the decision-quality formula. Bump on ANY change to the
# formula, benchmarks, or cohort definition — the 2026-07-19 re-benchmark moved
# the score 39.4 → 77.8 with zero system change, and without a version stamp
# that jump is indistinguishable from an actual improvement.
#   v3 (2026-07-19): benchmarked scale (wr/0.60, ECE honesty + discrimination,
#       pf/2), FLATs excluded, resolved_at window.
#   v4 (2026-07-19): HOLD claims tracked — calibration cohort now includes
#       HOLD_CORRECT/HOLD_MISS ("all claims"); directional win rate unchanged;
#       hold_accuracy + cohort provenance surfaced.
SCORE_VERSION = "v4"


def _audit_decisions(cycle_id: str, cycle_summary: dict) -> dict:
    """Score decision quality from resolved trade outcomes (rolling window).

    score = win_rate_score*0.4 + calibration_score*0.3 + risk_score*0.3, where
    each term is benchmarked so the 0-100 scale is interpretable:
      - win_rate_score: ex-flat 7-day directional accuracy / 0.60 (capped).
        60%+ sustained = full credit (decided-only baseline is a coin flip).
      - calibration_score: 0.7*honesty (1 - 2*ECE over confidence deciles)
        + 0.3*discrimination (high-conf beats low-conf). Uniform stated
        confidence caps this term at 0.85 even when perfectly honest.
      - risk_score: profit factor / 2.0 (capped). PF 2.0+ = full credit.

    Interpretation: ~90 = sustained 58-60% win rate with honest,
    differentiated confidence and PF ≥1.9 — an excellent desk. ~75-85 = solid.
    ~60-75 = mixed. Below 50 = the outcomes argue against the process.
    """
    buy = cycle_summary.get("buy_count", 0)
    sell = cycle_summary.get("sell_count", 0)
    hold = cycle_summary.get("hold_count", 0)
    total = buy + sell + hold
    issues = []
    outcome_stats = {}

    if total == 0:
        # No decisions this cycle, but check if we have historical outcomes to score from
        try:
            with get_db() as db:
                hist_count = db.execute(
                    "SELECT COUNT(*) FROM decision_outcomes WHERE resolved_at IS NOT NULL AND outcome != 'CANCELED'"
                ).fetchone()
                if hist_count and hist_count[0] >= 3:
                    # Fall through to the outcome-based scoring below
                    issues.append({"issue": "No decisions produced this cycle (using historical outcomes)", "severity": "info"})
                else:
                    return {"score": 0, "issues": [{"issue": "No decisions produced and no historical outcomes to score", "severity": "warning"}]}
        except Exception:
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
            # Window on resolved_at, not created_at: a created_at window would
            # oversample fast-resolving stop-outs (losses resolve in days, wins
            # in weeks) and read structurally pessimistic. The cohort-age gap
            # this leaves is surfaced via median_decision_age_days instead.
            resolved = db.execute(
                """
                SELECT action, confidence, pnl_pct, outcome,
                       EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)) / 86400.0
                FROM decision_outcomes
                WHERE resolved_at IS NOT NULL AND outcome != 'CANCELED' AND resolved_at > CURRENT_TIMESTAMP - INTERVAL '30 days'
                ORDER BY resolved_at DESC LIMIT 100
                """,
            ).fetchall()

            if len(resolved) >= 3:
                wins = [r for r in resolved if r[3] == "WIN"]
                losses = [r for r in resolved if r[3] == "LOSS"]
                flats = [r for r in resolved if r[3] == "FLAT"]
                holds_correct = [r for r in resolved if r[3] == "HOLD_CORRECT"]
                holds_miss = [r for r in resolved if r[3] == "HOLD_MISS"]
                # FLAT = position closed without a meaningful move — no verdict
                # on the call, so it belongs in neither numerator nor denominator.
                decided = wins + losses
                # HOLD claims ("no meaningful move") are checkable too, but they
                # stay OUT of the directional win rate — folding "price stayed
                # flat" into win rate would let low volatility read as skill.
                # They join the CALIBRATION cohort below: a stated confidence is
                # a stated confidence regardless of the claim's direction.
                hold_claims = holds_correct + holds_miss
                claims = decided + hold_claims
                _CORRECT = {"WIN", "HOLD_CORRECT"}

                win_rate = len(wins) / len(decided) if decided else 0.5
                avg_win_pnl = sum(r[2] for r in wins) / len(wins) if wins else 0
                avg_loss_pnl = sum(abs(r[2]) for r in losses) / len(losses) if losses else 0

                # Benchmarked, not raw: with the ±1% FLAT band the decided-only
                # baseline is a coin flip, and sustained 60% directional
                # accuracy at a 7-day horizon is top-tier for a systematic
                # desk. Raw scaling made 90+ require a ~90% win rate, which no
                # real desk posts — the score read as "failing" forever.
                WIN_RATE_BENCHMARK = 0.60
                win_rate_score = min(1.0, win_rate / WIN_RATE_BENCHMARK)

                # A confidence bucket below this size is noise: one lucky
                # low-conf trade must not be able to zero the whole term.
                MIN_BUCKET = 5

                def _bucket_win_rate(bucket):
                    return len([r for r in bucket if r[3] in _CORRECT]) / len(bucket)

                # Calibration = honesty (0.7) + discrimination (0.3).
                # Honesty: expected calibration error — |stated - realized| per
                # confidence decile, sample-weighted; full credit at 0, none at
                # ≥0.5. Discrimination: do high-conf calls beat low-conf calls
                # (gap on ≥MIN_BUCKET buckets, neutral 0.5 otherwise)? ECE
                # alone is gameable by stating the base rate on every trade;
                # the discrimination term means uniform confidence caps this
                # term at 0.85 — full credit requires differentiating AND
                # being right.
                buckets: dict = {}
                for r in claims:
                    if r[1] is None:
                        continue
                    buckets.setdefault((int(r[1]) // 10) * 10, []).append(r)
                qualified = {b: rows for b, rows in buckets.items() if len(rows) >= MIN_BUCKET}
                ece = None
                if qualified:
                    n_qual = sum(len(rows) for rows in qualified.values())
                    ece = sum(
                        len(rows) / n_qual
                        * abs((sum(x[1] for x in rows) / len(rows)) / 100.0 - _bucket_win_rate(rows))
                        for rows in qualified.values()
                    )
                honesty_score = max(0.0, 1.0 - 2.0 * ece) if ece is not None else 0.5

                high_conf = [r for r in claims if r[1] is not None and r[1] >= 70]
                low_conf = [r for r in claims if r[1] is not None and r[1] < 50]
                if len(high_conf) >= MIN_BUCKET and len(low_conf) >= MIN_BUCKET:
                    discrimination_score = min(1.0, max(0.0, 0.5 + _bucket_win_rate(high_conf) - _bucket_win_rate(low_conf)))
                else:
                    discrimination_score = 0.5

                calibration_score = 0.7 * honesty_score + 0.3 * discrimination_score

                if avg_loss_pnl > 0:
                    profit_factor = avg_win_pnl / avg_loss_pnl
                    risk_score = min(1.0, profit_factor / 2.0)
                elif avg_win_pnl > 0:
                    risk_score = 1.0
                else:
                    risk_score = 0.3

                score = (win_rate_score * 0.4 + calibration_score * 0.3 + risk_score * 0.3)

                # float() matters: EXTRACT(EPOCH ...) comes back as Decimal,
                # which survives round() and later kills strict json.dumps.
                ages = sorted(float(r[4]) for r in resolved if r[4] is not None)
                median_age_days = ages[len(ages) // 2] if ages else None

                hold_accuracy = (
                    len(holds_correct) / len(hold_claims) if hold_claims else None
                )

                outcome_stats = {
                    "score_version": SCORE_VERSION,
                    "total_resolved": len(resolved),
                    "wins": len(wins),
                    "losses": len(losses),
                    "flats": len(flats),
                    "holds_correct": len(holds_correct),
                    "holds_miss": len(holds_miss),
                    "hold_accuracy": round(hold_accuracy, 3) if hold_accuracy is not None else None,
                    "win_rate": round(win_rate, 3),
                    "win_rate_basis": "ex_flat_ex_hold",
                    "win_rate_benchmark": WIN_RATE_BENCHMARK,
                    "avg_win_pnl": round(avg_win_pnl, 2),
                    "avg_loss_pnl": round(avg_loss_pnl, 2),
                    "calibration_score": round(calibration_score, 3),
                    "calibration_basis": "all_claims_incl_holds",
                    "calibration_ece": round(ece, 3) if ece is not None else None,
                    "calibration_honesty": round(honesty_score, 3),
                    "calibration_discrimination": round(discrimination_score, 3),
                    "risk_score": round(risk_score, 3),
                    # Cohort provenance: the rolling terms are only comparable
                    # across cycles when the cohort is. When these shift, score
                    # movement is cohort drift, not system change.
                    "cohort_n": len(resolved),
                    "cohort_window_days": 30,
                    "median_decision_age_days": round(median_age_days, 1) if median_age_days is not None else None,
                    "scoring_method": "outcome_based",
                }

                if median_age_days is not None and median_age_days > 14:
                    issues.append({
                        "issue": f"Stale cohort: median scored decision is {median_age_days:.0f}d old (resolution lag) — score reflects past-era decisions, not current ones",
                        "severity": "info",
                    })
                if win_rate < 0.40 and decided:
                    issues.append({"issue": f"Low win rate: {win_rate:.0%} ({len(wins)}/{len(decided)} ex-flat)", "severity": "critical"})
                if avg_loss_pnl > 0 and avg_win_pnl < avg_loss_pnl:
                    issues.append({"issue": f"Avg loss ({avg_loss_pnl:.1f}%) > avg win ({avg_win_pnl:.1f}%)", "severity": "warning"})
                if ece is not None and ece > 0.15:
                    issues.append({
                        "issue": f"Conviction miscalibrated: stated confidence off realized win rate by {ece:.0%} on average",
                        "severity": "warning",
                    })
                if hold_accuracy is not None and len(hold_claims) >= 10 and hold_accuracy < 0.5:
                    issues.append({
                        "issue": f"HOLD calls miss: {hold_accuracy:.0%} of holds stayed inside the ±1% band "
                                 f"({len(holds_correct)}/{len(hold_claims)}) — the desk is holding through real moves",
                        "severity": "warning",
                    })

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

    # Per-cycle judge subscore: the rolling outcome terms cannot move on a
    # single cycle (30d cohort), which made 16 consecutive cycles score
    # byte-identically. This is the fast, this-cycle-only signal alongside it.
    per_cycle_judge = None
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT AVG(final_quality_score), COUNT(*) FROM decision_evaluations "
                "WHERE cycle_id = %s AND final_quality_score IS NOT NULL",
                [cycle_id],
            ).fetchone()
            if row and row[1]:
                per_cycle_judge = round(float(row[0]) * 20.0, 1)  # 0-5 → 0-100
    except Exception:
        pass

    return {
        "score": round(score, 3),
        "score_version": SCORE_VERSION,
        "per_cycle_judge_score": per_cycle_judge,
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
