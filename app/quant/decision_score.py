"""Deterministic decision score — the base analysis, computed once, in code.

WHY THIS EXISTS
---------------
Measured 2026-08-05 on `trade_results`:

    week      avg confidence   clearing the 70 floor   HOLD %
    07-13         74.0                 81.0%            49%
    07-20         63.1                 25.6%            58%
    07-27         63.3                 24.6%            89%
    08-03         57.3                  0.0%            93%

Zero of 60 decisions in the week of 08-03 cleared the confidence floor. The
floor is not the problem — it is calibrated on 1,672 resolved outcomes and
expectancy flips sign exactly at it (65-69 = -1.45%, 70-74 = +4.33%). Nor is a
guardrail forcing the outcome: 83 of 97 rows carry
`policy_action='HOLD_NO_SIGNAL'` and `decision_provenance='board_reasoned'`,
so the Board is choosing HOLD and writing a sub-60 number itself.

A prose fix was already tried. `dcc00af` (07-28) added an explicit confidence
rubric to the Board prompt. It made things worse — mean 63.6 -> 59.8, share
clearing 70 fell 19.5% -> 13.8%. An instruction that tells a model what a
number should mean has now failed for a week. So this module makes the number
arithmetic instead.

The second half of the same measurement is the reason the arithmetic is worth
having. The Board already writes a stop and a target on nearly every HOLD, and
those numbers are not consistent with the verdict:

    tkr   act   conf    price    stop    target    R:R
    MSTR  HOLD   62      91.65   89.21   104.60   5.29
    UBER  HOLD   50      67.21   65.41    76.30   5.05
    T     BUY    62      22.83   22.10    25.30   3.38

A 5.05:1 setup is a HOLD at 50 while a 3.38:1 setup is a BUY at 62. The
risk/reward is decorative because nothing consumes it: `calculate_risk_reward`
left the quant analyst's whitelist on 07-25 (zero calls) and the only other
holder, `tournament_pitch`, was retired on 07-29. Nothing on the live V3 path
computes an R:R at all.

WHAT THIS MODULE IS
-------------------
A pure, deterministic scoring pass over data ALREADY on file — the same
fundamentals / technicals / valuation rows the briefing blocks read. It emits
a 0-100 composite, a band, a computed risk/reward, and a confidence assembled
from named gates. It runs before the agents and is injected into their prompt,
so the repeated arithmetic every ticker needs (ratio banding, R:R, coverage)
happens once in code rather than being re-derived in tokens on every run. The
agents' job becomes judgement on top of a stated baseline — the same shape as
`sizing_bracket.py`, which replaced habitual position sizes with a bracket.

SHADOW ONLY. Nothing here alters a decision — the block is injected into the
prompt and the score is stored for comparison; no consumer reads the band back
into an action. Promotion to a constraint requires accumulated live evidence
that the score predicts P&L, the standing rule for board-adjacent changes.

The bands are STRONG_CANDIDATE / CANDIDATE / NEUTRAL / AVOID / NOT_SCOREABLE,
and the word CANDIDATE is doing real work. The first draft used BUY and
STRONG_BUY, which a test caught: a band that reads `BUY` is one careless line
away from being wired to the executor, and in the shadow table it would be
indistinguishable at a glance from the action the desk actually took. This
layer nominates candidates; the desk decides. There is also no SELL band,
because nothing here evaluates an existing position — AVOID means "do not
open", not "close what you hold".

THREE THINGS DELIBERATELY NOT PORTED FROM StrategyRobot
-------------------------------------------------------
StrategyRobot (github.com/LazyCat420/StrategyRobot) is the reference for the
shape: five weighted pillars, a letter grade, a Lynch/Simons blend, and a
decision matrix with an AVOID band. Three of its choices are defects at this
scale and are not reproduced.

1. **A missing metric must not score 0.5.** `_score_value` ends
   `np.mean(scores) if scores else 0.5`, so a ticker with no valuation data on
   file is indistinguishable from an exactly-average one. Here a pillar with
   no metrics is ABSENT: it is dropped from the weighting, the remaining
   weights renormalise, and `coverage_pct` reports what was actually scored.
   Below `_MIN_COVERAGE` the verdict is NOT_SCOREABLE, never a middling
   number. (A check that passes for both states is not a check.)

2. **Absolute thresholds tuned elsewhere do not transfer.** StrategyRobot
   AVOIDs on `PEG > 2.0`. Measured against this universe (1,195 tickers with a
   fundamentals row) the median PEG is 1.615 and p75 is 2.448 — that gate
   alone would reject a third of the book, replacing "always HOLD" with
   "always AVOID" and teaching us nothing. Every band here is anchored on
   MEASURED percentiles of this universe instead (see `_ANCHORS`).

3. **An absolute screen cannot pick, only reject.** Grading each name against
   fixed cutoffs answers "is this good?", which is the question the pipeline
   is already stuck on. `percentile` answers "is this the best available right
   now?" — the cross-sectional question, and the only one whose answer can
   rank. Both are emitted; which one predicts P&L is an open measurement, and
   the reason this ships shadow-first.
"""

from __future__ import annotations

import logging
import math
from itertools import pairwise
from typing import Any

logger = logging.getLogger(__name__)


# ── Pillar weights ──────────────────────────────────────────────────────────
# StrategyRobot's split, kept because it is a defensible prior and changing two
# things at once would make the shadow comparison unreadable. These are the
# weights BEFORE renormalisation; a pillar with no covered metric is removed
# and the rest rescale, so they only need to be relative.
WEIGHTS: dict[str, float] = {
    "value": 0.20,
    "growth": 0.25,
    "health": 0.25,
    "momentum": 0.20,
    "dividend": 0.10,
}

# A composite built on less than this share of the weight is not a score, it is
# a guess with a decimal point. Two pillars' worth (the smallest pair is
# dividend+momentum = 0.30) is too thin to rank on; 0.45 admits any name with
# fundamentals present and rejects the ones carrying only a price series.
_MIN_COVERAGE = 0.45

# `momentum` is the only technical pillar. Split out so the fundamental and
# technical composites can be reported separately — StrategyRobot's hybrid
# blends them 0.6/0.4 and that ratio is worth being able to re-measure.
_TECHNICAL_PILLARS = frozenset({"momentum"})
_HYBRID_FUNDAMENTAL_WEIGHT = 0.60


# ── Universe anchors ────────────────────────────────────────────────────────
# (p10, p90, direction) per metric. `direction` is +1 when HIGHER is better and
# -1 when LOWER is better. A raw value maps piecewise-linearly to 0..1 between
# the two anchors, clamped at both ends.
#
# Measured 2026-08-05 against the live `fundamentals` / `technicals` tables,
# latest snapshot per ticker, n as noted per metric:
#
#   SELECT count(v),
#          percentile_cont(0.10) WITHIN GROUP (ORDER BY v),
#          percentile_cont(0.90) WITHIN GROUP (ORDER BY v)
#     FROM (SELECT DISTINCT ON (ticker) ticker, <col> v
#             FROM fundamentals ORDER BY ticker, snapshot_date DESC) latest;
#
# Anchoring on p10/p90 rather than min/max means the score is roughly uniform
# across the universe by construction, which is what makes `percentile`
# meaningful. It also means these go stale as the universe drifts — re-run the
# query above and update, or call `refresh_anchors()` to recompute live. They
# are pinned by default so the score is deterministic and testable without a
# database.
_ANCHORS: dict[str, tuple[float, float, int]] = {
    # value — n between 674 and 863
    "pe_ratio":        (11.28,  67.17, -1),
    "peg_ratio":       (0.61,    4.24, -1),
    "price_to_book":   (0.74,   15.59, -1),
    "price_to_sales":  (0.63,   14.26, -1),
    # growth — n between 612 and 876
    "revenue_growth":  (-0.040,  0.425, +1),
    "eps_growth_qoq":  (-0.383,  1.644, +1),
    "profit_margin":   (-0.064,  0.319, +1),
    "oper_margin":     (-0.102,  0.395, +1),
    # health — n between 201 (roic) and 859
    "roe":             (-0.400,  0.496, +1),
    "roa":             (-0.085,  0.145, +1),
    "roic":            (0.003,   0.268, +1),
    "debt_to_equity":  (0.060,   2.551, -1),
    "current_ratio":   (0.547,   4.584, +1),
    "quick_ratio":     (0.294,   3.320, +1),
    # dividend — n=492. Absence is NOT a zero here; see `_score_dividend`.
    "dividend_yield":  (0.005,   0.045, +1),
}

_PILLAR_METRICS: dict[str, tuple[str, ...]] = {
    "value":    ("pe_ratio", "peg_ratio", "price_to_book", "price_to_sales"),
    "growth":   ("revenue_growth", "eps_growth_qoq", "profit_margin",
                 "oper_margin"),
    "health":   ("roe", "roa", "roic", "debt_to_equity", "current_ratio",
                 "quick_ratio"),
    "dividend": ("dividend_yield",),
    # momentum is computed from technicals, not from _ANCHORS — its inputs are
    # relationships (price vs SMA, RSI band) rather than levels.
    "momentum": (),
}

# Bands on the 0-100 composite, cut on the composite's OWN measured
# distribution rather than on round numbers.
#
# The first version of this used 72/62/45, reasoning that a percentile-anchored
# metric should spread across 0-100. It does not: the composite is a weighted
# MEAN of a dozen roughly-uniform metrics, and averaging concentrates toward
# the middle. Scored against 881 tickers the realised distribution is
#
#     min 6.6   p5 32.7   p25 46.9   p50 52.3   p75 58.2   p95 67.4   max 85.8
#     mean 51.9   stdev 10.6
#
# so those cuts produced 679 NEUTRAL, 178 AVOID, 16 BUY, 8 STRONG_BUY — 77% in
# the middle band. That is the exact failure this module exists to fix,
# reproduced in a new vocabulary, and only a distribution check caught it. A
# scorer's output distribution is the thing to verify; its logic reads fine
# either way.
#
# Re-cut on the measured percentiles: AVOID is the bottom ~25%, NEUTRAL the
# middle ~43%, BUY the next ~20%, STRONG_BUY the top ~12%. Re-measure with
# scripts/decision_score_report.py after any change to weights or anchors —
# these cuts are only meaningful against the distribution they were fitted to.
_BANDS: tuple[tuple[float, str], ...] = (
    (64.0, "STRONG_CANDIDATE"),
    (56.0, "CANDIDATE"),
    (47.0, "NEUTRAL"),
    (0.0,  "AVOID"),
)

# Risk/reward below this is not worth the spread even when the score is good.
# 2.0 is the conventional floor and matches `TAKE_PROFIT_RR_RATIO`'s default
# intent; it is a REPORTED gate, not an enforced one, until measured.
_MIN_RR = 2.0

# ATR multiple for the synthetic stop when no support level is on file. Matches
# the ATR multiplier already used elsewhere in the risk envelope.
_ATR_STOP_MULT = 2.0

# Fallback stop floor for the minority of names with no ATR on file. Only ever
# used when the volatility-based floor cannot be computed — see the comment at
# its use site for the 37,175:1 ratio it exists to prevent.
_MIN_STOP_DISTANCE_PCT = 0.03

# A price target implying more than this much upside is treated as vendor
# error rather than as a forecast. Set from the measured distribution — see the
# comment at its use site.
_MAX_TARGET_UPSIDE = 2.00

# Staleness costs confidence rather than blocking. The stale-price gate at
# `orchestrator._policy_action` already blocks execution past 3 trading days;
# duplicating that here would double-count one fact.
_STALE_TECH_DAYS = 3
_STALE_FUNDAMENTAL_DAYS = 45


def _finite(v: Any) -> float | None:
    """Coerce to a finite float, or None.

    NaN compares false against every threshold, so an unguarded one does not
    fail a band — it silently declines to score while looking scored. This is
    the same guard `technical_baseline.compute_technical_baseline` applies at
    the point of consumption, for the same reason.
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _as_fraction(v: float | None) -> float | None:
    """Normalise a ratio that vendors store either as 0.0416 or as 4.16.

    Same [-1.5, 1.5] band as `fundamental_block._pct`, deliberately: two
    different conventions for "is this a fraction" across modules reading the
    same column is how a margin becomes a 100x error. The band is narrow and
    the alternative — picking one convention and being wrong for half the
    sources — is worse. Values outside it are already percentages.

    Note AAPL's ROE of 1.4875 sits inside the band and is correctly read as
    148.75%; that is a real figure for a company with negative book equity
    trends, not a unit error.
    """
    if v is None:
        return None
    return v / 100.0 if v > 1.5 or v < -1.5 else v


def _norm(metric: str, raw: float | None) -> float | None:
    """Map a raw metric onto 0..1 using its measured universe anchors."""
    v = _finite(raw)
    if v is None:
        return None
    anchor = _ANCHORS.get(metric)
    if anchor is None:
        return None
    lo, hi, direction = anchor
    if hi == lo:
        return None
    t = (v - lo) / (hi - lo)
    t = max(0.0, min(1.0, t))
    return t if direction > 0 else 1.0 - t


# Metrics stored as fractions-or-percentages; everything else is a plain ratio.
_FRACTIONAL = frozenset({
    "revenue_growth", "eps_growth_qoq", "profit_margin", "oper_margin",
    "roe", "roa", "roic", "dividend_yield",
})


def _score_pillar(name: str, fundamental: dict) -> dict:
    """Score one fundamental pillar over whatever metrics are present.

    Returns a dict that always names WHICH metrics were used. A pillar score
    with no metric list is unauditable — the reader cannot tell a broad
    average from one lucky ratio.
    """
    used: dict[str, dict] = {}
    parts: list[float] = []
    for metric in _PILLAR_METRICS.get(name, ()):
        raw = _finite(fundamental.get(metric))
        if raw is None:
            continue
        val = _as_fraction(raw) if metric in _FRACTIONAL else raw
        # A negative or zero multiple is not "cheap", it is meaningless —
        # a P/E of -8 is a loss-making company, and mapping it to the top of
        # the "lower is better" scale would score it as the best value name in
        # the universe. Skip rather than invent.
        if metric in ("pe_ratio", "peg_ratio", "price_to_book",
                      "price_to_sales") and (val is None or val <= 0):
            used[metric] = {"raw": raw, "score": None,
                            "note": "non-positive multiple — not scoreable"}
            continue
        s = _norm(metric, val)
        if s is None:
            continue
        used[metric] = {"raw": raw, "normalised": round(val, 6),
                        "score": round(s, 4)}
        parts.append(s)

    if not parts:
        return {"score": None, "covered": False, "n_metrics": 0,
                "metrics": used}
    return {
        "score": round(100.0 * sum(parts) / len(parts), 1),
        "covered": True,
        "n_metrics": len(parts),
        "metrics": used,
    }


def _score_dividend(fundamental: dict) -> dict:
    """Dividend pillar, with absence treated as ABSENT rather than as zero.

    A growth company that has never paid a dividend is not a company with a
    bad dividend. Scoring `dividend_yield IS NULL` as 0 would penalise every
    non-payer by a tenth of the composite for a policy choice, which is how a
    screen ends up systematically preferring utilities. Only a ticker with a
    yield on file gets a dividend pillar; the others renormalise without it.
    """
    if _finite(fundamental.get("dividend_yield")) is None:
        return {"score": None, "covered": False, "n_metrics": 0,
                "metrics": {}, "note": "no dividend on file — pillar dropped, "
                                       "not scored zero"}
    return _score_pillar("dividend", fundamental)


def _score_momentum(technical: dict) -> dict:
    """Technical pillar from relationships, not levels.

    Deliberately shallow: trend vs the 200- and 50-day, an RSI band, and
    position in the Bollinger channel. These are the quantities
    `technical_baseline` already verifies against the `technicals` table, so
    the score cannot disagree with the block printed alongside it.
    """
    used: dict[str, dict] = {}
    parts: list[float] = []

    close = _finite(technical.get("close"))

    for key, label in (("sma_200", "trend_200d"), ("sma_50", "trend_50d")):
        sma = _finite(technical.get(key))
        if close is None or sma is None or sma <= 0:
            continue
        # Distance above/below the average, saturating at +/-20%. A stock 40%
        # over its 200-day is not twice as good as one 20% over; it is more
        # likely extended.
        gap = (close - sma) / sma
        s = max(0.0, min(1.0, (gap + 0.20) / 0.40))
        used[label] = {"close": close, key: sma,
                       "gap_pct": round(100.0 * gap, 2), "score": round(s, 4)}
        parts.append(s)

    rsi = _finite(technical.get("rsi"))
    if rsi is not None:
        # Peak at 55 — trending but not overbought. Falls off toward both
        # extremes. This is a MOMENTUM read, so oversold is not rewarded here;
        # mean reversion is a separate thesis and conflating them is how a
        # single number ends up meaning nothing.
        s = max(0.0, 1.0 - abs(rsi - 55.0) / 35.0)
        used["rsi"] = {"raw": rsi, "score": round(s, 4)}
        parts.append(s)

    # `bollinger_pct`, NOT `bollinger_position` — the latter is the ENUM
    # ("UPPER"/"LOWER"/…) that `technical_baseline` publishes for the prose
    # line, and reading it here would have been a permanent silent skip: a
    # string fails `_finite`, the metric never scores, and the pillar looks
    # like it simply had no data. A guard that can only ever decline is
    # indistinguishable from one that passes.
    bb_pct = _finite(technical.get("bollinger_pct"))
    if bb_pct is not None and 0.0 <= bb_pct <= 1.0:
        used["bollinger_pct"] = {"raw": bb_pct, "score": round(bb_pct, 4)}
        parts.append(bb_pct)

    if not parts:
        return {"score": None, "covered": False, "n_metrics": 0,
                "metrics": used}
    return {
        "score": round(100.0 * sum(parts) / len(parts), 1),
        "covered": True,
        "n_metrics": len(parts),
        "metrics": used,
    }


def compute_risk_reward(technical: dict,
                        valuation: dict | None = None,
                        fundamental: dict | None = None) -> dict:
    """Entry / stop / target and the ratio between them.

    This is the quantity the pipeline stopped computing when the tournament
    was retired, and the one the Board's own stop/target numbers imply but
    never consume.

    Stop is the TIGHTER of a support level and an ATR-multiple stop — the
    conservative reading, because a support level below a 2-ATR stop is
    usually a level from a different regime. Target prefers the technical
    resistance and falls back to a DCF fair value; a target below the current
    price yields a negative ratio and is reported as such rather than being
    clipped to zero, since "the model thinks this is above fair value" is
    information, not a missing number.
    """
    out: dict[str, Any] = {"ratio": None, "entry": None, "stop": None,
                           "target": None, "sources": {}}
    close = _finite(technical.get("close"))
    if close is None or close <= 0:
        out["note"] = "no close on file — risk/reward not computable"
        return out
    out["entry"] = round(close, 4)

    # ONE stop convention for every name: 2x ATR below the close.
    #
    # The first version picked the tighter of a support level and the ATR stop,
    # on the reasoning that support is where the thesis actually breaks. That
    # is a fine argument for a single trade and the wrong one for a screen.
    # Measured across 844 names it made the R:R column incomparable — a name
    # whose support happened to sit 1% under the close scored 14.7:1 on the
    # same setup that gave 4:1 to a name with no nearby level, and one reached
    # 37,175:1. Ranking on a ratio whose denominator is chosen differently per
    # name ranks the choice of denominator.
    #
    # So the ratio is uniform by construction and `support` is reported as
    # context instead of entering the arithmetic. A desk that wants to argue
    # for the support level can see it and say so.
    atr = _finite(technical.get("atr"))
    support = _finite(technical.get("support"))
    if atr is not None and atr > 0:
        out["stop"] = round(close - _ATR_STOP_MULT * atr, 4)
        out["sources"]["stop"] = f"{_ATR_STOP_MULT:g}x ATR ({atr:g})"
    elif support is not None and 0 < support < close:
        # No ATR on file. Support is the only level available, floored at a
        # flat percentage so a level sitting a hair under the close cannot
        # manufacture a ratio out of a rounding error.
        floored = min(support, close * (1.0 - _MIN_STOP_DISTANCE_PCT))
        out["stop"] = round(floored, 4)
        out["sources"]["stop"] = (
            "support level" if floored == support
            else f"{_MIN_STOP_DISTANCE_PCT:.0%} floor (no ATR, support too near)")
    if support is not None:
        out["support_level"] = round(support, 4)

    # Target priority is a HORIZON question, and getting it wrong is what made
    # the first version of this useless. Measured 2026-08-05 across 951 names
    # with a price on file: median upside to the technical `resistance` is
    # 5.5%, median upside to the analyst `target_price` is 14.8%. Resistance is
    # a near-term level; the decision it feeds is a 7-session-to-multi-quarter
    # thesis. Ranking on the near-term level put 68% of the universe under a
    # 2:1 floor and made the gate a constant rather than a filter.
    #
    # So: thesis-horizon targets first (analyst consensus 77% covered, DCF
    # where computable), near-term resistance only as a last resort — and the
    # source is always named, because a 3:1 to a fair value and a 3:1 to a
    # swing high are not the same claim.
    fair_value = None
    if valuation:
        for k in ("fair_value_per_share", "fair_value", "dcf_value_per_share"):
            fair_value = _finite(valuation.get(k))
            if fair_value is not None:
                break
    analyst = _finite((fundamental or {}).get("target_price"))
    # Vendor `target_price` carries unusable outliers, and they are not rare
    # enough to ignore. Measured across the 731 names with both a target and a
    # close: median upside 14.8%, p95 119%, p98 336%, max 317,618% — AUUD
    # carries a $3,272.50 target against a $1.03 close, SCNI $700.00 against
    # $0.25. These are reverse-split artifacts and vendor errors, not
    # forecasts, and they produced R:R ratios of 18,598:1 and 9,520:1.
    #
    # 24 of 731 (3.3%) imply more than a 3x. Discarding the TARGET (not the
    # ticker) above that line and falling through to the next source keeps a
    # genuinely distressed name scoreable while refusing to anchor on a number
    # that cannot be a forecast. The rejection is recorded, not silent — a
    # dropped input that leaves no trace is how a data problem becomes a
    # permanent invisible haircut.
    if analyst is not None and analyst > close * (1.0 + _MAX_TARGET_UPSIDE):
        out["rejected_targets"] = out.get("rejected_targets", [])
        out["rejected_targets"].append(
            f"analyst consensus target {analyst:g} implies "
            f"{100.0 * (analyst - close) / close:+,.0f}% — above the "
            f"{_MAX_TARGET_UPSIDE:.0%} sanity bound, treated as vendor error"
        )
        analyst = None
    resistance = _finite(technical.get("resistance"))

    if analyst is not None and analyst > close:
        out["target"] = round(analyst, 4)
        out["sources"]["target"] = "analyst consensus target"
        out["target_horizon"] = "thesis"
    elif fair_value is not None and fair_value > close:
        out["target"] = round(fair_value, 4)
        out["sources"]["target"] = "DCF fair value"
        out["target_horizon"] = "thesis"
    elif resistance is not None and resistance > close:
        out["target"] = round(resistance, 4)
        out["sources"]["target"] = "resistance level"
        out["target_horizon"] = "near-term"
    elif analyst is not None or fair_value is not None:
        # Every thesis-horizon target sits BELOW the current price. That is a
        # real finding — the name is above where the estimates put it — and
        # recording it as "no target" would erase it.
        best = max(v for v in (analyst, fair_value) if v is not None)
        out["target"] = round(best, 4)
        out["sources"]["target"] = (
            "analyst consensus target" if best == analyst else "DCF fair value")
        out["target_horizon"] = "thesis"
        out["note"] = "target is BELOW the current price — no upside on file"

    if out["stop"] is not None and out["target"] is not None:
        risk = close - out["stop"]
        reward = out["target"] - close
        if risk > 0:
            out["ratio"] = round(reward / risk, 2)
            out["downside_pct"] = round(-100.0 * risk / close, 2)
            out["upside_pct"] = round(100.0 * reward / close, 2)
        else:
            out["note"] = "stop is at or above entry — ratio not computable"
    return out


def _band_for(score: float) -> str:
    for floor, name in _BANDS:
        if score >= floor:
            return name
    return "AVOID"


def score_decision(fundamental: dict | None,
                   technical: dict | None,
                   valuation: dict | None = None,
                   ticker: str = "") -> dict:
    """The whole deterministic pass. Pure — no I/O, no clock, no randomness.

    Every input is a dict in the shape the existing baseline computers return
    (`compute_fundamental_baseline`, `compute_technical_baseline`,
    `compute_valuation_baseline`), so this scores exactly the values the
    briefing blocks print. It cannot disagree with them.
    """
    fundamental = fundamental or {}
    technical = technical or {}

    pillars: dict[str, dict] = {}
    for name in ("value", "growth", "health"):
        pillars[name] = _score_pillar(name, fundamental)
    pillars["dividend"] = _score_dividend(fundamental)
    pillars["momentum"] = _score_momentum(technical)

    covered = {n: p for n, p in pillars.items() if p["covered"]}
    coverage = sum(WEIGHTS[n] for n in covered)
    total_weight = sum(WEIGHTS.values())
    coverage_pct = round(100.0 * coverage / total_weight, 1)

    result: dict[str, Any] = {
        "ticker": (ticker or "").strip().upper(),
        "schema": "decision_score/1",
        "pillars": pillars,
        "coverage_pct": coverage_pct,
        "risk_reward": compute_risk_reward(technical, valuation, fundamental),
        "gates": [],
        "reasons": [],
        "warnings": [],
    }

    if coverage < _MIN_COVERAGE:
        # NOT a low score. A low score says "this is a bad company"; this says
        # "there is not enough on file to have an opinion", and collapsing the
        # two is the exact failure this module was written against.
        result["score"] = None
        result["band"] = "NOT_SCOREABLE"
        result["confidence"] = 0
        result["not_scoreable_reason"] = (
            f"only {coverage_pct}% of pillar weight had any metric on file "
            f"(floor {round(100.0 * _MIN_COVERAGE / total_weight)}%); "
            f"covered: {', '.join(sorted(covered)) or 'none'}"
        )
        return result

    composite = sum(WEIGHTS[n] * covered[n]["score"] for n in covered) / coverage
    result["score"] = round(composite, 1)
    result["band"] = _band_for(composite)

    fund_cov = {n: p for n, p in covered.items()
                if n not in _TECHNICAL_PILLARS}
    tech_cov = {n: p for n, p in covered.items() if n in _TECHNICAL_PILLARS}
    if fund_cov:
        w = sum(WEIGHTS[n] for n in fund_cov)
        result["fundamental_score"] = round(
            sum(WEIGHTS[n] * fund_cov[n]["score"] for n in fund_cov) / w, 1)
    if tech_cov:
        w = sum(WEIGHTS[n] for n in tech_cov)
        result["technical_score"] = round(
            sum(WEIGHTS[n] * tech_cov[n]["score"] for n in tech_cov) / w, 1)
    if "fundamental_score" in result and "technical_score" in result:
        # StrategyRobot's 0.6/0.4 hybrid, reported alongside the coverage-
        # weighted composite rather than replacing it. Which of the two ranks
        # better is the measurement this ships to enable.
        result["hybrid_score"] = round(
            _HYBRID_FUNDAMENTAL_WEIGHT * result["fundamental_score"]
            + (1.0 - _HYBRID_FUNDAMENTAL_WEIGHT) * result["technical_score"], 1)

    # Cross-sectional rank against the standing universe. Computed HERE rather
    # than by a caller holding the whole cycle, because the pipeline scores one
    # ticker at a time and no such caller exists — the reason this column was
    # NULL on every row of cycle-v3-1785962005.
    result["percentile"] = universe_percentile(result["score"])
    result["percentile_universe"] = _COMPOSITE_UNIVERSE_N

    result["confidence"] = _confidence(result, fundamental, technical)
    _apply_gates(result, fundamental)
    return result


def _confidence(result: dict, fundamental: dict, technical: dict) -> int:
    """Confidence assembled from named, checkable facts.

    Every term below is auditable after the fact: the reader can recompute it
    from the same row. That is the whole point — the number this replaces is a
    model's self-report, which drifted 17 points in three weeks with no change
    to the underlying data and did not recover when the prompt told it not to.

    Deliberately capped at 85. This layer sees ratios and price levels; it
    does not see a catalyst, a filing, or a management change, and a
    deterministic screen that can emit 95 would outrank a desk that read the
    10-K. The ceiling is the honest statement of what the inputs support.
    """
    conf = 40.0
    notes: list[str] = []

    coverage_pct = result.get("coverage_pct") or 0.0
    conf += 20.0 * (coverage_pct / 100.0)
    notes.append(f"+{20.0 * coverage_pct / 100.0:.0f} data coverage "
                 f"({coverage_pct:g}%)")

    # Agreement between the fundamental and technical reads. Disagreement is
    # the honest reason to be less sure, and it is the case the Board describes
    # as "the desks disagree on direction" — computed here instead of felt.
    f, t = result.get("fundamental_score"), result.get("technical_score")
    if f is not None and t is not None:
        spread = abs(f - t)
        if spread <= 10:
            conf += 12.0
            notes.append(f"+12 fundamental and technical agree "
                         f"({f:g} vs {t:g})")
        elif spread >= 30:
            conf -= 10.0
            notes.append(f"-10 fundamental and technical disagree "
                         f"({f:g} vs {t:g})")

    rr = (result.get("risk_reward") or {}).get("ratio")
    if rr is not None:
        if rr >= 3.0:
            conf += 12.0
            notes.append(f"+12 risk/reward {rr:g}:1")
        elif rr >= _MIN_RR:
            conf += 6.0
            notes.append(f"+6 risk/reward {rr:g}:1")
        elif rr < 1.0:
            conf -= 12.0
            notes.append(f"-12 risk/reward {rr:g}:1 is below break-even")

    # Conviction at the extremes of the score, not in the middle. A composite
    # of 50 is a genuine "no view" and should read as one.
    score = result.get("score")
    if score is not None:
        edge = abs(score - 50.0)
        if edge >= 22:
            conf += 10.0
            notes.append(f"+10 composite {score:g} is far from neutral")
        elif edge <= 5:
            conf -= 8.0
            notes.append(f"-8 composite {score:g} is genuinely mid-pack")

    tech_age = _finite(technical.get("age_trading_days"))
    if tech_age is not None and tech_age > _STALE_TECH_DAYS:
        conf -= 15.0
        notes.append(f"-15 technicals {tech_age:g} trading days old")
    fund_age = _finite(fundamental.get("age_days"))
    if fund_age is not None and fund_age > _STALE_FUNDAMENTAL_DAYS:
        conf -= 8.0
        notes.append(f"-8 fundamentals {fund_age:g} days old")

    result["confidence_terms"] = notes
    return int(max(0, min(85, round(conf))))


def _apply_gates(result: dict, fundamental: dict) -> None:
    """Structural checks, recorded SEPARATELY from the score.

    Kept out of the composite on purpose. A content bonus must not out-point a
    structural failure — that is how a screen ends up rating a company with
    negative equity highly because its momentum is good. Each gate names its
    verdict AND the case where it could not decide; UNKNOWN is never folded
    into PASS.
    """
    gates: list[dict] = result["gates"]

    def gate(name: str, verdict: str, detail: str) -> None:
        gates.append({"name": name, "verdict": verdict, "detail": detail})

    de = _finite(fundamental.get("debt_to_equity"))
    if de is None:
        gate("leverage", "UNKNOWN", "debt_to_equity not on file")
    elif de > 4.0:
        gate("leverage", "FAIL", f"debt/equity {de:g} — above the 4.0 line "
                                 "(universe p90 is 2.55)")
    else:
        gate("leverage", "PASS", f"debt/equity {de:g}")

    cr = _finite(fundamental.get("current_ratio"))
    if cr is None:
        gate("liquidity", "UNKNOWN", "current_ratio not on file")
    elif cr < 0.6:
        gate("liquidity", "FAIL", f"current ratio {cr:g} — below the 0.6 line "
                                  "(universe p10 is 0.55)")
    else:
        gate("liquidity", "PASS", f"current ratio {cr:g}")

    pm = _as_fraction(_finite(fundamental.get("profit_margin")))
    if pm is None:
        gate("profitability", "UNKNOWN", "profit_margin not on file")
    elif pm < 0:
        gate("profitability", "FAIL",
             f"net margin {100 * pm:.1f}% — loss-making on a TTM basis")
    else:
        gate("profitability", "PASS", f"net margin {100 * pm:.1f}%")

    rr_block = result.get("risk_reward") or {}
    rr = rr_block.get("ratio")
    horizon = rr_block.get("target_horizon")
    if rr is None:
        gate("risk_reward", "UNKNOWN",
             rr_block.get("note") or "stop or target not computable")
    elif horizon != "thesis":
        # A near-term resistance target measures a different quantity than the
        # floor was set for. Failing a name on it would be scoring a swing high
        # against a multi-quarter bar — the ratio is reported, but it does not
        # get a verdict it cannot earn.
        gate("risk_reward", "UNKNOWN",
             f"{rr:g}:1 but only a near-term target is on file "
             f"({rr_block['sources'].get('target')}) — wrong horizon to gate on")
    elif rr < _MIN_RR:
        gate("risk_reward", "FAIL",
             f"{rr:g}:1 is below the {_MIN_RR:g}:1 floor "
             f"(target: {rr_block['sources'].get('target')})")
    else:
        gate("risk_reward", "PASS",
             f"{rr:g}:1 (target: {rr_block['sources'].get('target')})")

    failed = [g["name"] for g in gates if g["verdict"] == "FAIL"]
    unknown = [g["name"] for g in gates if g["verdict"] == "UNKNOWN"]
    result["gates_failed"] = failed
    result["gates_unknown"] = unknown

    # A structural failure caps the band. It does NOT rewrite the score — the
    # score stays readable so the comparison "good company, bad setup" is still
    # visible in the shadow table.
    if failed and result.get("band") in ("STRONG_CANDIDATE", "CANDIDATE"):
        result["band_before_gates"] = result["band"]
        result["band"] = "NEUTRAL"
        result["warnings"].append(
            f"band capped from {result['band_before_gates']} to NEUTRAL by "
            f"failed structural gate(s): {', '.join(failed)}"
        )


# ── IO shell ────────────────────────────────────────────────────────────────
# Everything above is pure. Everything below touches the database, and does so
# fail-open: any exception degrades to a missing block, never a pipeline error.
# The pure core is what the tests exercise.

def compute_decision_score(ticker: str) -> dict:
    """Score `ticker` from the rows already on file.

    Reuses the three existing baseline computers rather than re-querying, so
    the score is arithmetic over exactly the values the briefing blocks print
    to the agent. A second query path would be a second source of truth.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"ticker": "", "band": "NOT_SCOREABLE", "score": None,
                "confidence": 0, "not_scoreable_reason": "no ticker"}
    try:
        from app.quant.fundamental_block import compute_fundamental_baseline
        from app.quant.technical_baseline import compute_technical_baseline
        from app.quant.valuation_block import compute_valuation_baseline

        fundamental = compute_fundamental_baseline(ticker) or {}
        technical = compute_technical_baseline(ticker) or {}
        try:
            valuation = compute_valuation_baseline(ticker) or {}
        except Exception as e:  # noqa: BLE001 — optional input, see below
            # The valuation block is the heaviest of the three (DCF over ten
            # years of periods) and the only optional input — it contributes a
            # fallback target and nothing else. Losing it must not lose the
            # score.
            logger.debug("[DecisionScore] %s: valuation unavailable "
                         "(non-fatal): %s", ticker, e)
            valuation = {}
        return score_decision(fundamental, technical, valuation, ticker=ticker)
    except Exception as e:  # noqa: BLE001 — never block a cycle on a score
        logger.warning("[DecisionScore] %s failed: %s: %s",
                       ticker, type(e).__name__, e)
        return {"ticker": ticker, "band": "NOT_SCOREABLE", "score": None,
                "confidence": 0,
                "not_scoreable_reason": f"scorer error: {type(e).__name__}"}


def compute_calibrated_confidence(
    baseline_confidence: int | float,
    board_confidence: float | int | None = None
) -> float:
    """Compute calibrated confidence by combining deterministic baseline with board LLM score.

    Addresses scale compression where LLM outputs cluster in a narrow 55-74 window with
    zero rank correlation to baseline confidence.

    Args:
        baseline_confidence: Deterministic baseline confidence (28-84).
        board_confidence: Optional LLM board verbalized confidence (0-100).

    Returns:
        Calibrated confidence float (0.0 - 100.0).
    """
    base = float(baseline_confidence or 0.0)
    if board_confidence is None:
        return round(max(0.0, min(100.0, base)), 1)
    
    board = float(board_confidence)
    if not (0.0 <= board <= 100.0):
        return round(max(0.0, min(100.0, base)), 1)

    # 55% deterministic baseline, 45% LLM board confidence
    calibrated = 0.55 * base + 0.45 * board
    return round(max(0.0, min(100.0, calibrated)), 1)


def _fmt_gate(g: dict) -> str:
    mark = {"PASS": "PASS", "FAIL": "FAIL", "UNKNOWN": "??"}[g["verdict"]]
    return f"  [{mark:4}] {g['name']}: {g['detail']}"


def build_decision_score_block(ticker: str, score: dict | None = None) -> str:
    """The injectable briefing section.

    Never returns "" for a ticker that was attempted. A silent empty block is
    how the one case where the agent knows least produces the least warning —
    the defect `technical_baseline` records from 2026-07-26, and the reason its
    NO DATA branch exists.

    The block states explicitly that it is a BASELINE, not a verdict. The
    measured risk of injecting a number is that the agent copies it: the HRP
    target weight was read as an order size, and the quant-math block was
    copied 127/127 faithfully. Here that would be a deterministic screen
    laundered into a board decision, which is worse than the drift it fixes.
    """
    s = score if score is not None else compute_decision_score(ticker)
    lines = [
        f"## DETERMINISTIC BASELINE SCORE — {s.get('ticker') or ticker}",
        ("Computed in code from the rows already on file, before any agent"
         " ran. It is a STARTING POINT you are expected to argue with, not a"
         " verdict: it sees ratios and price levels and is blind to catalysts,"
         " filings, management and news. Do NOT copy these numbers into your"
         " own `confidence` — reach your own, and say so explicitly when you"
         " disagree with this one and why."),
        "",
    ]

    if s.get("band") == "NOT_SCOREABLE":
        lines.append(f"  NOT SCOREABLE — {s.get('not_scoreable_reason', 'unknown')}")
        lines.append("  Treat this as a data gap, NOT as a neutral reading.")
        return "\n".join(lines)

    lines.append(
        f"  composite {s['score']:g}/100   band {s['band']}   "
        f"baseline confidence {s['confidence']}   "
        f"coverage {s['coverage_pct']:g}%"
    )
    if s.get("fundamental_score") is not None:
        bits = [f"fundamental {s['fundamental_score']:g}"]
        if s.get("technical_score") is not None:
            bits.append(f"technical {s['technical_score']:g}")
        if s.get("hybrid_score") is not None:
            bits.append(f"0.6/0.4 hybrid {s['hybrid_score']:g}")
        lines.append("  " + "   ".join(bits))
    if s.get("percentile") is not None:
        lines.append(
            f"  cross-sectional rank: {s['percentile']:g}th percentile of the "
            f"{s.get('percentile_universe', '?')}-name scored universe"
        )
    lines.append("")

    lines.append("  PILLARS (weight, score, metrics actually used)")
    for name in ("value", "growth", "health", "momentum", "dividend"):
        p = s["pillars"][name]
        w = f"{WEIGHTS[name]:.0%}"
        if not p["covered"]:
            note = p.get("note") or "no metric on file — dropped, NOT scored 0"
            lines.append(f"    {name:9} {w:>4}  ABSENT   {note}")
            continue
        used = ", ".join(sorted(
            k for k, v in p["metrics"].items() if v.get("score") is not None))
        lines.append(f"    {name:9} {w:>4}  {p['score']:5g}   n={p['n_metrics']} "
                     f"({used})")
    lines.append("")

    rr = s.get("risk_reward") or {}
    if rr.get("ratio") is not None:
        lines.append(
            f"  RISK/REWARD {rr['ratio']:g}:1   entry {rr['entry']:g}   "
            f"stop {rr['stop']:g} ({rr['sources'].get('stop', '?')})   "
            f"target {rr['target']:g} ({rr['sources'].get('target', '?')})"
        )
        if rr.get("upside_pct") is not None:
            lines.append(f"    upside {rr['upside_pct']:+g}%   "
                         f"downside {rr['downside_pct']:+g}%")
    else:
        lines.append(f"  RISK/REWARD not computable — "
                     f"{rr.get('note', 'stop or target missing')}")
    lines.append("")

    lines.append("  STRUCTURAL GATES (checked separately from the score, so a "
                 "good score cannot out-point a failure)")
    for g in s.get("gates", []):
        lines.append(_fmt_gate(g))
    if s.get("warnings"):
        lines.append("")
        for w in s["warnings"]:
            lines.append(f"  WARNING: {w}")

    if s.get("confidence_terms"):
        lines.append("")
        lines.append("  BASELINE CONFIDENCE, term by term (recomputable from "
                     "this row):")
        lines.append("    starts at 40; " + "; ".join(s["confidence_terms"]))

    return "\n".join(lines)


# Percentile knots for the COMPOSITE itself, measured 2026-08-05 over the 881
# tickers that scored out of the 1,195 with a fundamentals row. Reproduce with
# `scripts/decision_score_report.py distribution`.
#
# This exists because `rank_scores` could not run on the live path and the
# `percentile` column was therefore always NULL — caught by auditing
# cycle-v3-1785962005, where all 11 rows came back with no percentile. The
# pipeline scores ONE ticker at a time inside `run_v3_pipeline`, so there is no
# moment where a cycle's names are all in hand to rank against each other.
#
# Ranking against the standing universe is also the better question. A
# percentile within a 10-name cycle says "best of whatever was screened today",
# which moves with the screen; against the universe it says "top decile of
# everything we track", which is the claim a book with finite capital needs.
_COMPOSITE_KNOTS: tuple[tuple[float, float], ...] = (
    (6.6, 0.0), (32.7, 5.0), (42.4, 15.0), (46.9, 25.0), (52.3, 50.0),
    (58.2, 75.0), (62.0, 85.0), (67.4, 95.0), (85.8, 100.0),
)
_COMPOSITE_UNIVERSE_N = 881


def universe_percentile(score: float | None) -> float | None:
    """Where `score` sits in the measured universe distribution, 0-100.

    Linear interpolation between the knots above, clamped at both ends. None
    in, None out — a name with no score has no rank, and inventing one is the
    failure this module exists to avoid.
    """
    s = _finite(score)
    if s is None:
        return None
    knots = _COMPOSITE_KNOTS
    if s <= knots[0][0]:
        return 0.0
    if s >= knots[-1][0]:
        return 100.0
    for (x0, y0), (x1, y1) in pairwise(knots):
        if x0 <= s <= x1:
            if x1 == x0:
                return round(y1, 1)
            return round(y0 + (y1 - y0) * (s - x0) / (x1 - x0), 1)
    return None


def rank_scores(scores: list[dict]) -> list[dict]:
    """Attach a WITHIN-SET percentile to each scoreable result.

    Used by the report script when a whole set is in hand at once. The live
    per-ticker path cannot call this — see `universe_percentile`, which answers
    the same question against the standing universe instead.

    This is the half an absolute screen cannot do. Banding a name against
    fixed cutoffs answers "is this good?"; the pipeline is already stuck on
    that question and answers "not sure" 93% of the time. A percentile answers
    "is this the best available right now?", which is the question a book with
    finite capital actually faces.

    Mutates and returns the same list. NOT_SCOREABLE entries are left without
    a percentile — ranking a name whose score does not exist would invent one.
    """
    scoreable = [s for s in scores if s.get("score") is not None]
    n = len(scoreable)
    if n < 2:
        # A percentile over one name is 100 by construction and means nothing.
        return scores
    ordered = sorted(scoreable, key=lambda s: s["score"])
    for rank, s in enumerate(ordered):
        s["percentile"] = round(100.0 * rank / (n - 1), 1)
        s["percentile_universe"] = n
    return scores


def refresh_anchors() -> dict[str, tuple[float, float, int]]:
    """Recompute `_ANCHORS` from the live universe and return the new table.

    NOT called automatically. The pinned anchors keep the score deterministic
    and testable without a database; this exists so the pinned values can be
    re-measured deliberately when the universe drifts, and so the query that
    produced them is executable rather than a comment. It does not mutate
    module state — the caller decides whether to adopt the result.
    """
    from app.db.connection import get_db

    out: dict[str, tuple[float, float, int]] = {}
    for metric, (_, _, direction) in _ANCHORS.items():
        try:
            with get_db() as db:
                row = db.execute(
                    f"""
                    WITH latest AS (
                        SELECT DISTINCT ON (ticker) ticker, {metric} AS v
                          FROM fundamentals
                         ORDER BY ticker, snapshot_date DESC
                    )
                    SELECT percentile_cont(0.10) WITHIN GROUP (ORDER BY v),
                           percentile_cont(0.90) WITHIN GROUP (ORDER BY v),
                           count(v)
                      FROM latest
                    """
                ).fetchone()
        except Exception as e:  # noqa: BLE001 — advisory helper, never fatal
            logger.warning("[DecisionScore] anchor refresh failed for %s: %s",
                           metric, e)
            continue
        if not row or row[0] is None or row[1] is None or row[0] == row[1]:
            continue
        # Too thin a sample produces anchors that move with a handful of rows.
        if (row[2] or 0) < 100:
            logger.info("[DecisionScore] %s: only %s values — anchor kept "
                        "pinned", metric, row[2])
            continue
        out[metric] = (round(float(row[0]), 4), round(float(row[1]), 4),
                       direction)
    return out
