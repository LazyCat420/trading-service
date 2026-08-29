"""Computed macro trend block — deterministic grounding for the Regime Engine.

The Regime Engine scores seven 0.0-1.0 factors, but the LIVE MACRO SNAPSHOT it
reads is a list of *latest closes* — one number per instrument. Three of those
factors are not computable from that input at all:

    trend_strength   "SPY/QQQ directional clarity" — needs a slope, not a level
    sector_momentum  "breadth of current rotation" — needs sector changes
    liquidity        "depth/breadth health"        — needs breadth

Measured over 14 days (366 runs) the engine made **zero** tool calls and still
returned trend_strength averaging 0.81 and sector_momentum 0.64: confident
scores derived from data it never had. This module computes those inputs from
the 9 months of daily closes already in ``asset_prices`` and injects them, so
the scores are read off real numbers instead of invented.

Same precompute-inject pattern as the quant desk's PRECOMPUTED QUANT MATH: an
agent that averages 1.0 loops will not fetch what it needs, so it has to
already be on the desk.
"""

from __future__ import annotations

import logging
import math
from app.db import mongo_query
from app.utils.numeric import finite as _finite

logger = logging.getLogger(__name__)

# Index symbols as stored by market_regime_collector (yfinance ^-prefix stripped).
_INDEXES = [
    ("GSPC", "S&P 500"),
    ("IXIC", "Nasdaq"),
    ("RUT", "Russell 2000"),
]

_SECTORS = [
    ("XLK", "Tech"), ("XLF", "Financials"), ("XLE", "Energy"),
    ("XLV", "Health"), ("XLY", "Discretionary"), ("XLP", "Staples"),
    ("XLI", "Industrials"), ("XLB", "Materials"), ("XLU", "Utilities"),
    ("XLRE", "Real Estate"), ("XLC", "Communications"),
]

# ~6 months of trading days — enough for a stable VIX z-score without reaching
# back into a different volatility regime.
_ZSCORE_LOOKBACK = 126


def _load_series(symbols: list[str], lookback: int) -> dict[str, list[float]]:
    """Closes per symbol, oldest→newest, NaNs dropped."""
    from app.db import mongo_query

    series: dict[str, list[float]] = {}
    for sym in symbols:
        try:
            rows = mongo_query.find_rows('asset_prices', {'symbol': sym}, ['close'], sort=[('date', -1)], limit=lookback)
        except Exception as e:  # noqa: BLE001 — grounding is advisory
            logger.debug("[MacroTrend] %s series fetch failed: %s", sym, e)
            continue
        closes = [c for c in (_finite(r[0]) for r in rows) if c is not None]
        if closes:
            series[sym] = list(reversed(closes))
    return series


def _pct_change(closes: list[float], days: int) -> float | None:
    """Percent change over `days` trading days."""
    if len(closes) <= days:
        return None
    prev = closes[-1 - days]
    if not prev:
        return None
    return (closes[-1] - prev) / prev * 100.0


def _sma_distance(closes: list[float], window: int) -> float | None:
    """Percent distance of the latest close from its `window`-day SMA."""
    if len(closes) < window:
        return None
    sma = sum(closes[-window:]) / window
    if not sma:
        return None
    return (closes[-1] - sma) / sma * 100.0


def _zscore(closes: list[float]) -> float | None:
    if len(closes) < 20:
        return None
    mean = sum(closes) / len(closes)
    var = sum((c - mean) ** 2 for c in closes) / len(closes)
    sd = math.sqrt(var)
    if sd < 1e-9:
        return None
    return (closes[-1] - mean) / sd


def _percentile(closes: list[float]) -> float | None:
    if len(closes) < 20:
        return None
    latest = closes[-1]
    below = sum(1 for c in closes if c < latest)
    return below / len(closes) * 100.0


def build_macro_trend_lines() -> list[str]:
    """Return the computed-trend briefing lines (empty list on any failure).

    Never raises: a grounding failure must degrade the regime call, not abort
    the cycle.
    """
    try:
        symbols = ["VIX", "VIX3M", "DX", "TNX"] + [s for s, _ in _INDEXES] + [s for s, _ in _SECTORS]
        series = _load_series(symbols, _ZSCORE_LOOKBACK)
        if not series:
            return []

        lines: list[str] = []

        # ── Volatility: level alone can't say whether this is calm-for-now or
        # calm-relative-to-history. z-score and percentile can.
        vix = series.get("VIX")
        if vix:
            parts = [f"VIX {vix[-1]:.2f}"]
            chg5 = _pct_change(vix, 5)
            if chg5 is not None:
                parts.append(f"5d {chg5:+.1f}%")
            z = _zscore(vix)
            if z is not None:
                parts.append(f"6m z-score {z:+.2f}")
            pct = _percentile(vix)
            if pct is not None:
                parts.append(f"{pct:.0f}th percentile of 6m")
            lines.append("- " + " | ".join(parts))

        # VIX term structure: spot above 3-month is the classic stress tell.
        vix3m = series.get("VIX3M")
        if vix and vix3m and vix3m[-1]:
            ratio = vix[-1] / vix3m[-1]
            shape = ("BACKWARDATION (stress — spot above 3m)" if ratio > 1.0
                     else "contango (normal)")
            lines.append(f"- VIX term structure: spot/3m = {ratio:.3f} — {shape}")

        # ── Trend strength: slope + distance from the 50d mean.
        for sym, label in _INDEXES:
            closes = series.get(sym)
            if not closes:
                continue
            parts = [f"{label} {closes[-1]:,.2f}"]
            for days, tag in ((5, "5d"), (20, "20d")):
                chg = _pct_change(closes, days)
                if chg is not None:
                    parts.append(f"{tag} {chg:+.2f}%")
            dist = _sma_distance(closes, 50)
            if dist is not None:
                parts.append(f"{dist:+.2f}% vs SMA-50")
            lines.append("- " + " | ".join(parts))

        # ── Sector breadth + rotation dispersion: the actual inputs for
        # sector_momentum and the breadth half of liquidity.
        moves: list[tuple[str, float]] = []
        for sym, label in _SECTORS:
            closes = series.get(sym)
            if not closes:
                continue
            chg = _pct_change(closes, 5)
            if chg is not None:
                moves.append((label, chg))
        if moves:
            positive = sum(1 for _, m in moves if m > 0)
            moves.sort(key=lambda x: x[1], reverse=True)
            dispersion = moves[0][1] - moves[-1][1]
            lines.append(
                f"- Sector breadth (5d): {positive}/{len(moves)} sectors positive | "
                f"dispersion {dispersion:.2f}pp "
                f"(best {moves[0][0]} {moves[0][1]:+.2f}%, worst {moves[-1][0]} {moves[-1][1]:+.2f}%)"
            )
            lines.append(
                "- Sector 5d moves: "
                + ", ".join(f"{name} {chg:+.1f}%" for name, chg in moves)
            )

        # ── Dollar and rates: the engine outputs dxy_trend / yield_trend as
        # words; without a change it was naming a direction from one level.
        dxy = series.get("DX")
        if dxy:
            chg = _pct_change(dxy, 5)
            lines.append(
                f"- US Dollar (DXY) {dxy[-1]:.2f}"
                + (f" | 5d {chg:+.2f}%" if chg is not None else "")
            )
        tnx = series.get("TNX")
        if tnx:
            # TNX is quoted in percent (e.g. 4.67 = 4.67%); a 5d delta in basis
            # points is how a desk actually reads it.
            bps = (tnx[-1] - tnx[-6]) * 100 if len(tnx) > 5 else None
            lines.append(
                f"- 10Y yield {tnx[-1]:.2f}%"
                + (f" | 5d {bps:+.0f}bps" if bps is not None else "")
            )

        return lines
    except Exception as e:  # noqa: BLE001 — never block a cycle on grounding
        logger.warning("[MacroTrend] Failed to build computed trend block: %s", e)
        return []


def build_macro_trend_block() -> str:
    """The formatted section, or "" when nothing could be computed."""
    lines = build_macro_trend_lines()
    if not lines:
        return ""
    return (
        "COMPUTED MACRO TREND (deterministic, from daily closes — "
        "these are measured, not estimated):\n" + "\n".join(lines)
    )
