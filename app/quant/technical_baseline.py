"""Verified technical baseline — the numbers the quant desk must not invent.

The 2026-07-24 audit traced every RSI the Quant/Risk Analyst reported back to
the text it was given. Of 305 reports carrying an RSI, only 134 matched any
number on the desk; **171 did not**, and 148 of those came from runs that made
ZERO tool calls. Measured examples: IP reported 58.0 against a desk value of
71.19; GOOGL reported 47.0 against 53.7.

Those numbers are not decoration. `risk_metrics` drives volatility_regime and
stop placement, and the Board and Synthesizer read them as fact.

The root cause is a category error, not a bad prompt: RSI, ATR, Bollinger
position and SMA status are *computed* quantities that already sit in the
`technicals` table (148k rows, 503 tickers, refreshed daily). Asking a language
model to restate them invites exactly this failure, and no amount of "cite your
source" wording removes the opportunity. So the pipeline computes them, injects
them, and — in agent_runner — overwrites the artifact's numeric fields with the
verified values, recording any disagreement instead of trusting it.

The model keeps every genuinely interpretive field: volatility_regime,
thesis_direction, position sizing, overlays, the narrative.
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

# A baseline older than this is reported WITH its age rather than silently
# presented as current — several tickers lag the daily refresh by days.
_STALE_AFTER_DAYS = 5

# Fields we can verify deterministically. Anything not listed here stays the
# model's to judge.
VERIFIED_NUMERIC_FIELDS = ("rsi", "atr")
VERIFIED_ENUM_FIELDS = ("sma_200_status", "bollinger_position", "volume_trend")


def _finite(val) -> float | None:
    """None unless `val` is a real, finite number.

    NaN survives a `NOT NULL` check and compares false against every
    threshold, so an unfiltered one lands in risk_metrics looking like data.
    """
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _fetch_technicals(ticker: str) -> dict | None:
    from app.db.connection import get_db

    with get_db() as db:
        row = db.execute(
            """
            SELECT date, rsi_14, atr_14, bb_upper, bb_mid, bb_lower,
                   sma_50, sma_200, support, resistance
            FROM technicals WHERE ticker = %s ORDER BY date DESC LIMIT 1
            """,
            [ticker],
        ).fetchone()
    if not row:
        return None
    keys = ("date", "rsi_14", "atr_14", "bb_upper", "bb_mid", "bb_lower",
            "sma_50", "sma_200", "support", "resistance")
    return {
        k: (v if k == "date" else _finite(v))
        for k, v in zip(keys, row)
    }


def _fetch_price_and_volume(ticker: str) -> tuple[float | None, str | None]:
    """Latest close, and a volume trend read from the last 20 sessions."""
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            "SELECT close, volume FROM price_history WHERE ticker = %s "
            "ORDER BY date DESC LIMIT 20",
            [ticker],
        ).fetchall()
    if not rows:
        return None, None

    try:
        close = float(rows[0][0])
        if close != close:
            close = None
    except (TypeError, ValueError):
        close = None

    volumes = []
    for _, vol in rows:
        try:
            v = float(vol)
        except (TypeError, ValueError):
            continue
        if v == v and v > 0:
            volumes.append(v)

    trend = None
    if len(volumes) >= 10:
        recent = sum(volumes[:5]) / 5
        baseline = sum(volumes[5:]) / len(volumes[5:])
        if baseline > 0:
            ratio = recent / baseline
            trend = ("INCREASING" if ratio > 1.15
                     else "DECREASING" if ratio < 0.85 else "STABLE")
    return close, trend


def compute_technical_baseline(ticker: str) -> dict:
    """Verified indicator values for `ticker`.

    Returns {} when nothing could be established — callers must treat an empty
    dict as "no verified baseline", never as "no risk".
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {}
    try:
        tech = _fetch_technicals(ticker)
        if not tech:
            return {}

        # Sanitize HERE, where the values are consumed — not only in the
        # fetcher. NaN compares false against every threshold, so a single one
        # slipping through would put `nan` straight into risk_metrics and read
        # as a real number to everything downstream.
        tech = {
            k: (v if k == "date" else _finite(v))
            for k, v in tech.items()
        }

        close, volume_trend = _fetch_price_and_volume(ticker)
        close = _finite(close)

        baseline: dict = {"as_of": tech["date"], "source": "technicals table"}
        if tech.get("rsi_14") is not None:
            baseline["rsi"] = round(tech["rsi_14"], 2)
        if tech.get("atr_14") is not None:
            baseline["atr"] = round(tech["atr_14"], 2)
        if tech.get("support") is not None:
            baseline["support"] = round(tech["support"], 2)
        if tech.get("resistance") is not None:
            baseline["resistance"] = round(tech["resistance"], 2)
        if close is not None:
            baseline["close"] = round(close, 2)
        if volume_trend:
            baseline["volume_trend"] = volume_trend

        sma_200 = tech.get("sma_200")
        if close is not None and sma_200:
            baseline["sma_200"] = round(sma_200, 2)
            baseline["sma_200_status"] = "ABOVE" if close >= sma_200 else "BELOW"
        sma_50 = tech.get("sma_50")
        if sma_50:
            baseline["sma_50"] = round(sma_50, 2)

        upper, lower = tech.get("bb_upper"), tech.get("bb_lower")
        if close is not None and upper and lower and upper > lower:
            span = upper - lower
            pos = (close - lower) / span
            baseline["bollinger_pct"] = round(pos, 3)
            # Price can sit OUTSIDE the bands, which is the most informative
            # case of all — a raw "-52% of the band" reads as a bug, so name it.
            if pos > 1.0:
                baseline["bollinger_position"] = "UPPER"
                baseline["bollinger_note"] = "above the upper band (extended)"
            elif pos < 0.0:
                baseline["bollinger_position"] = "LOWER"
                baseline["bollinger_note"] = "below the lower band (extended)"
            else:
                baseline["bollinger_position"] = (
                    "UPPER" if pos >= 0.8 else "LOWER" if pos <= 0.2 else "MIDDLE"
                )

        try:
            age = (date.today() - tech["date"]).days
            baseline["age_days"] = age
            baseline["stale"] = age > _STALE_AFTER_DAYS
        except Exception:
            baseline["stale"] = False

        return baseline
    except Exception as e:  # noqa: BLE001 — never block a cycle on grounding
        logger.warning("[TechnicalBaseline] %s failed: %s", ticker, e)
        return {}


def build_technical_baseline_block(ticker: str) -> str:
    """The injectable briefing section, or "" when nothing is verified."""
    b = compute_technical_baseline(ticker)
    if not b:
        return ""

    lines = [
        "VERIFIED TECHNICAL BASELINE (computed from stored daily data — "
        "these are the authoritative values; do NOT restate them from memory "
        "or estimate around them):"
    ]
    if b.get("stale"):
        lines.append(
            f"  ⚠ STALE: latest stored session is {b['as_of']} "
            f"({b.get('age_days')} days old) — treat levels as indicative and "
            f"say so in data_gaps."
        )
    else:
        lines.append(f"  as of {b['as_of']}")

    if "close" in b:
        lines.append(f"  - close: {b['close']}")
    if "rsi" in b:
        lines.append(f"  - RSI-14: {b['rsi']}")
    if "atr" in b:
        lines.append(f"  - ATR-14: {b['atr']}")
    if "sma_200_status" in b:
        lines.append(f"  - price vs SMA-200 ({b.get('sma_200')}): {b['sma_200_status']}")
    if "sma_50" in b:
        lines.append(f"  - SMA-50: {b['sma_50']}")
    if "bollinger_position" in b:
        note = b.get("bollinger_note")
        detail = note if note else f"{b['bollinger_pct']:.0%} of the band"
        lines.append(f"  - Bollinger position: {b['bollinger_position']} ({detail})")
    if "volume_trend" in b:
        lines.append(f"  - volume trend (5d vs prior 15d): {b['volume_trend']}")
    if "support" in b:
        lines.append(f"  - stored support: {b['support']}")
    if "resistance" in b:
        lines.append(f"  - stored resistance: {b['resistance']}")

    return "\n".join(lines)


def reconcile_risk_metrics(
    artifact: dict, ticker: str, *, model_used_tools: bool = False
) -> dict:
    """Replace the artifact's verifiable risk_metrics with computed values.

    Returns a report of what disagreed, so fabrication stays measurable
    instead of merely suppressed:

        {"corrected": {"rsi": {"model": 58.0, "verified": 71.19}}, ...}

    The model's original values are preserved on the artifact under
    `_model_reported_metrics` — the point is to stop the bad number reaching
    the Board, not to hide that it was produced.

    `model_used_tools` guards against trading one wrong number for another. The
    `technicals` table lags the daily refresh for most tickers (measured: GOOGL
    7 days, IP 9, NVDA 7), so overwriting a value the agent genuinely fetched
    live with a week-old stored one would be its own regression. The rule:

      - baseline fresh          → correct (it is authoritative)
      - stale, agent used NO tools → correct anyway; the agent had no source at
        all, and a real stale number beats an invented one. Flagged as a gap.
      - stale, agent used tools    → do NOT overwrite; record the discrepancy
        for audit and let the fresher fetch stand.
    """
    if not isinstance(artifact, dict):
        return {}
    metrics = artifact.get("risk_metrics")
    if not isinstance(metrics, dict):
        return {}

    baseline = compute_technical_baseline(ticker)
    if not baseline:
        return {}

    stale = bool(baseline.get("stale"))
    apply_corrections = (not stale) or (not model_used_tools)

    corrected: dict = {}
    original: dict = {}

    for field in VERIFIED_NUMERIC_FIELDS:
        verified = baseline.get(field)
        if verified is None:
            continue
        stated = metrics.get(field)
        try:
            stated_f = float(stated)
        except (TypeError, ValueError):
            stated_f = None
        # 1.0 absolute tolerance: rounding and a one-session lag are fine,
        # a different number is not.
        if stated_f is None or abs(stated_f - verified) > 1.0:
            if stated_f is not None:
                corrected[field] = {"model": stated_f, "verified": verified}
                original[field] = stated_f
            if apply_corrections:
                metrics[field] = verified

    for field in VERIFIED_ENUM_FIELDS:
        verified = baseline.get(field)
        if not verified:
            continue
        stated = str(metrics.get(field) or "").strip().upper()
        if stated != verified:
            if stated:
                corrected[field] = {"model": stated, "verified": verified}
                original[field] = stated
            if apply_corrections:
                metrics[field] = verified

    if original and apply_corrections:
        artifact["_model_reported_metrics"] = original
    elif corrected:
        # Not corrected, but the disagreement is still evidence.
        artifact["_unreconciled_metrics"] = corrected

    if stale:
        artifact.setdefault("data_gaps", []).append(
            f"Estimate: stored technical baseline is {baseline.get('age_days')} "
            f"days old (as of {baseline['as_of']})"
        )

    return {
        "corrected": corrected,
        "applied": apply_corrections,
        "stale": stale,
        "as_of": str(baseline.get("as_of", "")),
    }
