import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db import mongo_query
from app.db import mongo_store

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
#   v5 (2026-07-31): discrimination is Kendall's tau over every qualified
#       confidence bucket, replacing a two-bucket (>=70 vs <50) comparison
#       that ignored 50-69 and could not see a fall-off inside >=70. Scores
#       either side of this change are NOT comparable on the calibration
#       term. Also reports which axis confidence tracks (frequency vs
#       magnitude) and excludes DEGRADED_ARTIFACT from the cohort. The
#       two-bucket term scored the live desk ~1.0 while tau says 0.50.
SCORE_VERSION = "v5"


def _bucket_rank_tau(qualified: dict, value_of) -> float | None:
    """Kendall's tau between bucket confidence and some per-bucket value.

    Concordant/discordant over bucket PAIRS rather than rows, because the
    question is whether the confidence SCALE is ordered, and rows inside one
    bucket all share a stated confidence. Ties contribute to neither.

    Returns None when fewer than two buckets qualify — no relationship is
    measurable from one point, and returning a number there would invent one.
    """
    points = sorted((conf, value_of(rows)) for conf, rows in qualified.items())
    if len(points) < 2:
        return None
    concordant = discordant = 0
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            delta = points[j][1] - points[i][1]
            if delta > 0:
                concordant += 1
            elif delta < 0:
                discordant += 1
    total = concordant + discordant
    if total == 0:
        return 0.0
    return (concordant - discordant) / total


def _rank_discrimination(qualified: dict, bucket_win_rate) -> tuple[float, float | None]:
    """(score in [0,1], raw tau). 0.5 means no discrimination, as before."""
    tau = _bucket_rank_tau(qualified, bucket_win_rate)
    if tau is None:
        return 0.5, None
    return (tau + 1.0) / 2.0, tau


# A tau this small is not an ordering, just sampling noise on a handful of
# buckets. Naming an axis below it would be the same overclaim the two-bucket
# term made.
_TAU_MEANINGFUL = 0.34


def _confidence_axis(win_tau: float | None, mag_tau: float | None) -> str:
    """Which axis is a confidence number carrying — frequency, or size?

    A confidence label claims "how often I will be right". If it orders P&L
    magnitude but not win rate, it is a conviction/size signal wearing a
    probability's name, and every consumer that reads it as P(win) — ECE,
    Brier, any sizing rule — is on the wrong axis.
    """
    w = (win_tau or 0.0) >= _TAU_MEANINGFUL
    m = (mag_tau or 0.0) >= _TAU_MEANINGFUL
    if w and m:
        return "both"
    if m:
        return "magnitude"
    if w:
        return "frequency"
    return "neither"


def _audit_decisions(cycle_id: str, cycle_summary: dict) -> dict:
    """Score decision quality from resolved trade outcomes (rolling window).

    score = win_rate_score*0.4 + calibration_score*0.3 + risk_score*0.3, where
    each term is benchmarked so the 0-100 scale is interpretable:
      - win_rate_score: ex-flat 7-day directional accuracy / 0.60 (capped).
        60%+ sustained = full credit (decided-only baseline is a coin flip).
      - calibration_score: 0.7*honesty (1 - 2*ECE over confidence deciles)
        + 0.3*discrimination — Kendall's tau over every qualified confidence
        bucket, rescaled to [0,1] with 0.5 = no ordering. ECE alone is
        gameable by stating the base rate on every trade, so full credit
        still requires differentiating AND being right; unlike the two-bucket
        term it replaced, an inversion anywhere on the scale is penalised.
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
            hist_count = mongo_query.agg_row('decision_outcomes', {'resolved_at': {'$ne': None}, 'outcome': {'$nin': ['CANCELED', 'DEGRADED_ARTIFACT']}}, [('count', None)])
            if hist_count and hist_count[0] >= 3:
                # Fall through to the outcome-based scoring below
                issues.append({"issue": "No decisions produced this cycle (using historical outcomes)", "severity": "info"})
            else:
                return {"score": 0, "issues": [{"issue": "No decisions produced and no historical outcomes to score", "severity": "warning"}]}
        except Exception:
            return {"score": 0, "issues": [{"issue": "No decisions produced", "severity": "critical"}]}


    try:
        rows = mongo_query.find_rows(
            'analysis_results',
            {'cycle_id': cycle_id, 'confidence': {'$ne': None}},
            ['confidence'],
        )
        if rows:
            confs = [r[0] for r in rows]
            if max(confs) - min(confs) < 10 and len(confs) >= 3:
                issues.append({"issue": f"Uniform confidence ({min(confs)}-{max(confs)})", "severity": "info"})
            if sum(confs) / len(confs) < 40:
                issues.append({"issue": f"Low avg confidence: {sum(confs) / len(confs):.0f}%", "severity": "warning"})
    except Exception:
        pass

    try:
        # Window on resolved_at, not created_at: a created_at window would
        # oversample fast-resolving stop-outs (losses resolve in days, wins
        # in weeks) and read structurally pessimistic. The cohort-age gap
        # this leaves is surfaced via median_decision_age_days instead.
        _now = datetime.now(timezone.utc)
        _raw = mongo_query.find_rows(
            'decision_outcomes',
            # One resolved_at clause carrying both predicates: a second
            # 'resolved_at' key would silently REPLACE the IS NOT NULL.
            # ($gt against a datetime already excludes missing/None.)
            {'resolved_at': {'$ne': None, '$gt': _now - timedelta(days=30)},
             'outcome': {'$nin': ['CANCELED', 'DEGRADED_ARTIFACT']}},
            ['action', 'confidence', 'pnl_pct', 'outcome', 'created_at'],
            sort=[('resolved_at', -1)], limit=100,
        )
        # r[4] was EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at))/86400
        # — a decision's age in DAYS. BSON subtraction yields a timedelta,
        # so convert via total_seconds(); returning the timedelta itself
        # would be a silent unit error downstream (float(r[4]) below).
        resolved = []
        for action, confidence, pnl_pct, outcome, created_at in _raw:
            age_days = None
            if created_at is not None:
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                age_days = (_now - created_at).total_seconds() / 86400.0
            resolved.append((action, confidence, pnl_pct, outcome, age_days))

        if len(resolved) >= 3:
            wins = [r for r in resolved if r[3] == "WIN"]
            losses = [r for r in resolved if r[3] == "LOSS"]
            flats = [r for r in resolved if r[3] == "FLAT"]
            holds_correct = [r for r in resolved if r[3] == "HOLD_CORRECT"]
            # Long-only: a hold through a DECLINE was right, and is graded
            # HOLD_AVOIDED_DECLINE by outcome_tracker._classify. Counted
            # with the correct holds, and reported separately below so the
            # "held flat" and "dodged a drop" populations stay legible.
            holds_avoided = [r for r in resolved if r[3] == "HOLD_AVOIDED_DECLINE"]
            holds_right = holds_correct + holds_avoided
            holds_miss = [r for r in resolved if r[3] == "HOLD_MISS"]
            # FLAT = position closed without a meaningful move — no verdict
            # on the call, so it belongs in neither numerator nor denominator.
            decided = wins + losses
            # HOLD claims ("no meaningful move") are checkable too, but they
            # stay OUT of the directional win rate — folding "price stayed
            # flat" into win rate would let low volatility read as skill.
            # They join the CALIBRATION cohort below: a stated confidence is
            # a stated confidence regardless of the claim's direction.
            hold_claims = holds_right + holds_miss
            claims = decided + hold_claims
            _CORRECT = {"WIN", "HOLD_CORRECT", "HOLD_AVOIDED_DECLINE"}

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

            # Discrimination across EVERY qualified bucket, not two of them.
            #
            # This used to be `win_rate(conf>=70) - win_rate(conf<50)`,
            # which is a two-value gate wearing a distribution's name: it
            # ignored 50-69 entirely, and — because it collapsed all of
            # >=70 into one bucket — it could not see a fall-off inside
            # that range. Measured 2026-07-31, that is exactly what was
            # hiding: win rate runs 74->63%, 85->66%, 91->72%, 95->46%.
            # The desk's most confident calls are its worst, and the old
            # term scored the desk as discriminating well throughout.
            # (With the floor at 70, the conf<50 arm is also usually too
            # small to qualify, so the term silently sat at neutral 0.5.)
            #
            # Kendall's tau over the same buckets ECE already uses: count
            # concordant vs discordant bucket pairs, so a single inversion
            # at the top is penalised no matter where it sits. tau=+1 is
            # perfectly ordered, -1 perfectly inverted, 0 no relationship;
            # rescaled to [0,1] with 0.5 = no discrimination, which keeps
            # the previous neutral value meaning the same thing.
            discrimination_score, conf_tau = _rank_discrimination(
                qualified, _bucket_win_rate
            )

            calibration_score = 0.7 * honesty_score + 0.3 * discrimination_score

            # Which axis is confidence tracking? The win-rate tau above
            # answers "more often"; this answers "bigger". Measured
            # 2026-07-31 it orders BOTH (win +0.50, |P&L| +0.64) — the
            # scale works. What is wrong is the LEVEL (15.8 points of
            # overstatement) and the top bucket (95 wins 45.5%). Reporting
            # both taus keeps that distinction visible instead of letting
            # one number stand for "calibration".
            mag_tau = _bucket_rank_tau(
                qualified,
                lambda rows: sum(abs(x[2] or 0.0) for x in rows) / len(rows),
            )

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
                len(holds_right) / len(hold_claims) if hold_claims else None
            )

            outcome_stats = {
                "score_version": SCORE_VERSION,
                "total_resolved": len(resolved),
                "wins": len(wins),
                "losses": len(losses),
                "flats": len(flats),
                "holds_correct": len(holds_correct),
                "holds_avoided_decline": len(holds_avoided),
                "holds_right": len(holds_right),
                "holds_miss": len(holds_miss),
                "hold_accuracy_basis": "direction_aware_long_only",
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
                # Raw taus behind the score, and which axis the number
                # actually carries. A confidence that orders magnitude but
                # not frequency is a conviction signal wearing a
                # probability's name; today it orders both, so the defect
                # is level and the top bucket, not the axis.
                "confidence_tau_win_rate": round(conf_tau, 3) if conf_tau is not None else None,
                "confidence_tau_magnitude": round(mag_tau, 3) if mag_tau is not None else None,
                "confidence_predicts": _confidence_axis(conf_tau, mag_tau),
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
                    # Say what the numerator IS. It is no longer "stayed
                    # inside the ±1% band" — it is that plus the holds that
                    # sat out a fall, which on a long-only book were right.
                    # The failure being flagged is specifically the desk
                    # holding through RALLIES it could have bought.
                    "issue": f"HOLD calls miss: only {hold_accuracy:.0%} of holds were right "
                             f"({len(holds_right)}/{len(hold_claims)} = {len(holds_correct)} flat "
                             f"+ {len(holds_avoided)} avoided a fall) — the desk is holding "
                             f"through {len(holds_miss)} rallies it could have bought",
                    "severity": "warning",
                })

            try:
                _backfill_cycle_summaries()
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
        row = mongo_query.agg_row('decision_evaluations', {'cycle_id': cycle_id, 'final_quality_score': {'$ne': None}}, [('avg', 'final_quality_score'), ('count', None)])
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

def _backfill_cycle_summaries() -> None:
    """NOT CONVERTED — deliberately a no-op, as it has always been in practice.

    The original was an `UPDATE cycle_summaries cs ... FROM decision_outcomes
    outcome` correlated by (ticker, action) plus a ±1 day BETWEEN range on
    cs.cycle_date vs outcome.created_at. That is a correlated multi-predicate
    join-update — a shape the mongo_query toolkit does not express (join_rows
    does ONE equality and is read-only), so translating it would mean
    inventing a near-miss.

    It also never actually ran: `outcome` aliased a table while `do` — the
    alias in the sibling scripts/decision_score_report.py — is a Postgres
    RESERVED WORD, and this statement raised a SyntaxError on every call
    (found 2026-08-07). `cycle_summaries.was_correct` and `.outcome_pnl` have
    therefore never been populated from decision_outcomes. Dropping the dead
    SQL changes no behaviour; a real backfill needs a deliberate design.
    """
    return None

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

        mongo_store.update_docs('autoresearch_cycle_summaries', {'cycle_id': cycle_id}, {'$set': {'total_tickers': len(analysis_results), 'buy_count': buy_count, 'sell_count': sell_count, 'hold_count': hold_count, 'avg_confidence': round(avg_conf, 1), 'top_ticker': top_ticker, 'top_confidence': top_confidence, 'lesson_summary': lesson[:500]}, '$setOnInsert': {'id': f"cs-{uuid.uuid4().hex[:12]}"}}, upsert=True)
    except Exception as e:
        logger.warning("cycle_summaries write failed (non-fatal): %s", e)
