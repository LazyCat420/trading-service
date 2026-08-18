"""
Freshness Gate — Programmatic pre-filter for the Portfolio Manager.

Pure MongoDB implementation for freshness_gate_config, analysis_results, and news_articles.
"""

import logging
from datetime import datetime, timezone

from app.db import mongo_query
from app.db import mongo_store

logger = logging.getLogger(__name__)

# ── Default thresholds (used if DB config not available) ──
_DEFAULTS = {
    "price_delta_max_pct": 5.0,
    "news_count_max": 2.0,
    "volume_ratio_max": 2.0,
    "rsi_boundary_weight": 1.0,
    "fund_delta_max": 3.0,
    "composite_threshold": 0.25,
}

# Weights for each signal dimension
_WEIGHTS = {
    "price_delta": 0.30,
    "news_delta": 0.25,
    "volume_ratio": 0.20,
    "rsi_boundary": 0.15,
    "fund_delta": 0.10,
}


def _load_thresholds() -> dict | None:
    """Load tunable thresholds from the freshness_gate_config collection."""
    try:
        rows = mongo_query.find_rows(
            'freshness_gate_config', {}, ['threshold_name', 'threshold_value', 'weight']
        )
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
    """Run the Freshness Gate on scored stocks."""
    config = _load_thresholds()
    composite_threshold = _get_threshold(config, "composite_threshold")

    # Fetch analysis snapshots for all tickers in one query
    tickers = [s["ticker"] for s in top_scorers]
    analysis_snapshots = {}

    if tickers:
        try:
            pipeline = [
                {"$match": {"ticker": {"$in": tickers}}},
                {"$sort": {"ticker": 1, "created_at": -1}},
                {
                    "$group": {
                        "_id": "$ticker",
                        "analysis_price": {"$first": "$analysis_price"},
                        "analysis_rsi": {"$first": "$analysis_rsi"},
                        "analysis_fund_count": {"$first": "$analysis_fund_count"},
                        "created_at": {"$first": "$created_at"},
                    }
                },
            ]
            docs = mongo_store.aggregate("analysis_results", pipeline)
            for d in docs:
                t = d.get("_id")
                if t:
                    analysis_snapshots[t] = {
                        "analysis_price": d.get("analysis_price"),
                        "analysis_rsi": d.get("analysis_rsi"),
                        "analysis_fund_count": d.get("analysis_fund_count") or 0,
                        "created_at": d.get("created_at"),
                    }
        except Exception as e:
            logger.warning("[FreshnessGate] Could not fetch analysis snapshots: %s", e)

    # Fetch news counts since last analysis for each ticker
    news_counts = {}
    if tickers:
        try:
            for ticker in tickers:
                snap = analysis_snapshots.get(ticker)
                if snap and snap.get("created_at"):
                    since = snap["created_at"]
                    if hasattr(since, "tzinfo") and since.tzinfo is None:
                        since = since.replace(tzinfo=timezone.utc)
                    c_val = mongo_store.count_docs(
                        "news_articles",
                        {"ticker": ticker, "published_at": {"$gt": since}},
                    )
                    news_counts[ticker] = c_val or 0
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
