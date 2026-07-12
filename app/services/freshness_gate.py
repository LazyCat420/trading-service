"""
Freshness Gate — Programmatic pre-filter for the Portfolio Manager.

Classifies each scored stock as NEW, CHANGED, or STALE before the PM agent
sees it. This eliminates the 3,500-token reasoning spiral where the LLM
tried to figure out recency rules itself.

Classification:
  - NEW:     Never analyzed (no entry in analysis_results)
  - CHANGED: Analyzed before, but material change detected (composite delta >= threshold)
  - STALE:   Analyzed recently, no material change → auto-skip

The composite delta score is computed across 5 signals:
  - Price delta %  (weight 0.30)
  - News delta     (weight 0.25)
  - Volume ratio   (weight 0.20)
  - RSI boundary   (weight 0.15)
  - Fund delta     (weight 0.10)

Thresholds are stored in freshness_gate_config table and are LLM-tunable.
"""

import logging
from datetime import datetime, timezone

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# ── Default thresholds (used if DB config not available) ──
_DEFAULTS = {
    "price_delta_max_pct": 5.0,
    "news_count_max": 3.0,
    "volume_ratio_max": 2.0,
    "rsi_boundary_weight": 1.0,
    "fund_delta_max": 3.0,
    "composite_threshold": 0.40,
}

# Weights for each signal dimension
_WEIGHTS = {
    "price_delta": 0.30,
    "news_delta": 0.25,
    "volume_ratio": 0.20,
    "rsi_boundary": 0.15,
    "fund_delta": 0.10,
}


def _load_thresholds() -> dict:
    """Load tunable thresholds from the freshness_gate_config table."""
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT threshold_name, threshold_value, weight FROM freshness_gate_config"
            ).fetchall()
            if rows:
                config = {}
                for name, value, weight in rows:
                    config[name] = {"value": value, "weight": weight}
                return config
    except Exception as e:
        logger.warning("[FreshnessGate] Could not load config from DB, using defaults: %s", e)
    return None


def _get_threshold(config: dict | None, name: str) -> float:
    """Get a threshold value from config or defaults."""
    if config and name in config:
        return config[name]["value"]
    return _DEFAULTS.get(name, 1.0)


def _compute_delta_score(
    stock: dict,
    last_analysis: dict | None,
    news_count: int,
    config: dict | None,
) -> tuple[float, str]:
    """Compute the composite freshness delta score for a stock.

    Returns (delta_score, reason_string).
    """
    price_delta_max = _get_threshold(config, "price_delta_max_pct")
    news_max = _get_threshold(config, "news_count_max")
    vol_max = _get_threshold(config, "volume_ratio_max")
    fund_max = _get_threshold(config, "fund_delta_max")

    signals = {}

    # 1. Price Delta %
    current_price = stock.get("price", 0)
    last_price = last_analysis.get("analysis_price") if last_analysis else None
    if last_price and last_price > 0 and current_price > 0:
        price_delta_pct = abs(current_price - last_price) / last_price * 100
        signals["price"] = min(price_delta_pct / price_delta_max, 1.0)
    else:
        signals["price"] = 0.0
        price_delta_pct = 0.0

    # 2. News Delta (articles since last analysis)
    signals["news"] = min(news_count / news_max, 1.0) if news_max > 0 else 0.0

    # 3. Volume Ratio
    vol_ratio = stock.get("rvol", 0)
    signals["volume"] = min(vol_ratio / vol_max, 1.0) if vol_max > 0 else 0.0

    # 4. RSI Boundary Cross
    current_rsi = stock.get("rsi", 50)
    last_rsi = last_analysis.get("analysis_rsi") if last_analysis else None
    rsi_crossed = 0.0
    if last_rsi is not None:
        # Crossed oversold boundary (30) or overbought boundary (70)
        crossed_30 = (last_rsi >= 30 and current_rsi < 30) or (last_rsi < 30 and current_rsi >= 30)
        crossed_70 = (last_rsi <= 70 and current_rsi > 70) or (last_rsi > 70 and current_rsi <= 70)
        rsi_crossed = 1.0 if (crossed_30 or crossed_70) else 0.0
    signals["rsi"] = rsi_crossed

    # 5. Institutional Fund Delta
    current_funds = stock.get("inst_funds", 0)
    last_funds = last_analysis.get("analysis_fund_count", 0) if last_analysis else 0
    fund_delta = abs(current_funds - last_funds)
    signals["funds"] = min(fund_delta / fund_max, 1.0) if fund_max > 0 else 0.0

    # Composite score
    delta_score = (
        signals["price"] * _WEIGHTS["price_delta"]
        + signals["news"] * _WEIGHTS["news_delta"]
        + signals["volume"] * _WEIGHTS["volume_ratio"]
        + signals["rsi"] * _WEIGHTS["rsi_boundary"]
        + signals["funds"] * _WEIGHTS["fund_delta"]
    )

    # Build reason string
    parts = []
    if signals["price"] > 0.3:
        parts.append(f"price Δ{price_delta_pct:.1f}%")
    if signals["news"] > 0.3:
        parts.append(f"{news_count} new articles")
    if signals["volume"] > 0.3:
        parts.append(f"vol {vol_ratio:.1f}x")
    if rsi_crossed:
        parts.append(f"RSI crossed ({last_rsi:.0f}→{current_rsi:.0f})")
    if signals["funds"] > 0.3:
        parts.append(f"fund Δ{fund_delta}")
    reason = ", ".join(parts) if parts else "no material change"

    return delta_score, reason


def run_freshness_gate(
    top_scorers: list[dict],
    last_analysis_map: dict,
    emit: object = None,
) -> dict:
    """Run the Freshness Gate on scored stocks.

    Args:
        top_scorers: List of scored stock dicts from the scoring engine.
        last_analysis_map: Dict mapping ticker -> last analysis datetime.
        emit: Optional SSE emitter for real-time logging.

    Returns:
        {
            "eligible": [stocks classified as NEW or CHANGED],
            "stale": [stocks classified as STALE with skip reasons],
        }
    """
    config = _load_thresholds()
    composite_threshold = _get_threshold(config, "composite_threshold")

    # Fetch analysis snapshots for all tickers in one query
    tickers = [s["ticker"] for s in top_scorers]
    analysis_snapshots = {}

    if tickers:
        try:
            with get_db() as db:
                placeholders = ",".join(["%s"] * len(tickers))
                rows = db.execute(
                    f"""
                    SELECT DISTINCT ON (ticker)
                        ticker, analysis_price, analysis_rsi, analysis_fund_count, created_at
                    FROM analysis_results
                    WHERE ticker IN ({placeholders})
                    ORDER BY ticker, created_at DESC
                    """,
                    tickers,
                ).fetchall()
                for row in rows:
                    analysis_snapshots[row[0]] = {
                        "analysis_price": row[1],
                        "analysis_rsi": row[2],
                        "analysis_fund_count": row[3] or 0,
                        "created_at": row[4],
                    }
        except Exception as e:
            logger.warning("[FreshnessGate] Could not fetch analysis snapshots: %s", e)

    # Fetch news counts since last analysis for each ticker
    news_counts = {}
    if tickers:
        try:
            with get_db() as db:
                for ticker in tickers:
                    snap = analysis_snapshots.get(ticker)
                    if snap and snap.get("created_at"):
                        since = snap["created_at"]
                        if since.tzinfo is None:
                            since = since.replace(tzinfo=timezone.utc)
                        count = db.execute(
                            "SELECT COUNT(*) FROM news_articles WHERE ticker = %s AND published_at > %s",
                            [ticker, since],
                        ).fetchone()[0]
                        news_counts[ticker] = count
                    else:
                        news_counts[ticker] = 0
        except Exception as e:
            logger.warning("[FreshnessGate] Could not fetch news counts: %s", e)

    eligible = []
    stale = []

    for stock in top_scorers:
        ticker = stock["ticker"]
        last_date = last_analysis_map.get(ticker)
        snap = analysis_snapshots.get(ticker)

        # NEW: never analyzed before
        if not last_date:
            stock["freshness"] = "NEW"
            stock["delta_score"] = 1.0
            stock["freshness_reason"] = "never analyzed"
            eligible.append(stock)
            logger.info("[FreshnessGate] NEW: %s (never analyzed)", ticker)
            continue

        # Compute composite delta score
        news_count = news_counts.get(ticker, 0)
        delta_score, reason = _compute_delta_score(stock, snap, news_count, config)
        stock["delta_score"] = delta_score
        stock["freshness_reason"] = reason

        if delta_score >= composite_threshold:
            stock["freshness"] = "CHANGED"
            eligible.append(stock)
            logger.info(
                "[FreshnessGate] CHANGED: %s (delta=%.2f, %s)",
                ticker, delta_score, reason,
            )
        else:
            stock["freshness"] = "STALE"
            stock["skip_reason"] = reason
            stale.append(stock)
            logger.info(
                "[FreshnessGate] STALE: %s (delta=%.2f, %s)",
                ticker, delta_score, reason,
            )

    logger.info(
        "[FreshnessGate] Result: %d eligible (NEW+CHANGED), %d stale",
        len(eligible), len(stale),
    )
    return {"eligible": eligible, "stale": stale}
