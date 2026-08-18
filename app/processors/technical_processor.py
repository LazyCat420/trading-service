"""
Technical Processor — compute indicators from price_history → technicals table.

Pure Python + ta library. No LLM calls. No hallucinations.
"""

import pandas as pd
import ta
import logging
from datetime import timedelta
from app.db import mongo_store

logger = logging.getLogger(__name__)

#: Mirrors app.quant.returns._FRESHNESS_LAG_DAYS.
_FRESHNESS_LAG_DAYS = 3

# Minimum sessions before the `ta` indicators are computable at all.
_MIN_SESSIONS = 28


def _one_vendor(ticker: str, query: dict) -> dict:
    """Pin a price_history filter to the ticker's dominant vendor.

    Resolved through THIS module's `mongo_store` rather than by importing
    app.quant.returns' resolver, so a caller (or a test) that redirects
    `technical_processor.mongo_store` redirects the vendor lookup with it.
    Importing the other module's helper split the read across two seams and
    sent the lookup to the real database while the window read a fake.

    Same rule as `dominant_source_sql`: freshest first, then deepest, then
    name. Returns the query unchanged when the ticker has one vendor or none.
    """
    try:
        stats = mongo_store.aggregate("price_history", [
            {"$match": {"ticker": ticker}},
            {"$group": {"_id": "$source", "n": {"$sum": 1}, "mx": {"$max": "$date"}}},
        ])
    except Exception:  # noqa: BLE001 — a vendor lookup must not kill the cycle
        return query
    if not stats or len(stats) <= 1:
        return query
    dated = [r["mx"] for r in stats if r.get("mx") is not None]
    if not dated:
        return query
    cutoff = max(dated) - timedelta(days=_FRESHNESS_LAG_DAYS)
    best = sorted(
        stats,
        key=lambda r: (not (r.get("mx") is not None and r["mx"] >= cutoff),
                       -int(r.get("n") or 0), str(r["_id"] or "")),
    )[0]["_id"]
    return {**query, "source": best} if best is not None else query


def compute_technicals(ticker: str, period: int = 500) -> int:
    """
    Compute all technical indicators for a ticker and write to technicals collection in MongoDB.
    Needs at least 28 rows.
    Returns number of rows written.
    """
    # ONE vendor. price_history is keyed (ticker, date, source), so an
    # unfiltered `limit=period` returns `period` ROWS spanning only ~period/2
    # DATES on a dual-source ticker, and mixes adjusted closes with raw ones
    # inside a single indicator window. The SQL this replaced pinned the
    # vendor inside the LIMIT subquery; the port dropped it.
    docs = mongo_store.find_docs(
        "price_history",
        _one_vendor(ticker.upper(), {"ticker": ticker.upper()}),
        sort=[("date", -1)],
        limit=period,
    )

    if not docs or len(docs) < _MIN_SESSIONS:
        logger.debug(
            "[tech] %s: not enough price data (%d rows, need >=%d)",
            ticker,
            len(docs) if docs else 0,
            _MIN_SESSIONS,
        )
        return 0

    # Reverse to chronological order (ASC)
    docs.reverse()
    df = pd.DataFrame(
        [
            {
                "date": d.get("date"),
                "open": d.get("open"),
                "high": d.get("high"),
                "low": d.get("low"),
                "close": d.get("close"),
                "volume": d.get("volume"),
            }
            for d in docs
        ]
    )

    # ── Trend indicators ──
    df["sma_20"] = ta.trend.sma_indicator(df["close"], window=20)
    df["sma_50"] = ta.trend.sma_indicator(df["close"], window=50)
    df["sma_200"] = ta.trend.sma_indicator(df["close"], window=200)
    df["ema_12"] = ta.trend.ema_indicator(df["close"], window=12)
    df["ema_26"] = ta.trend.ema_indicator(df["close"], window=26)

    # MACD
    macd = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # ADX
    adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
    df["adx_14"] = adx.adx()

    # ── Momentum indicators ──
    df["rsi_14"] = ta.momentum.rsi(df["close"], window=14)

    stoch = ta.momentum.StochasticOscillator(
        df["high"], df["low"], df["close"], window=14, smooth_window=3
    )
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()

    # ── Volatility indicators ──
    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()

    atr = ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=14
    )
    df["atr_14"] = atr.average_true_range()

    # ── Volume indicators ──
    df["obv"] = ta.volume.on_balance_volume(df["close"], df["volume"].astype(float))
    df["vwap"] = (df["close"] * df["volume"]).rolling(window=20, min_periods=1).sum() / df["volume"].rolling(window=20, min_periods=1).sum()

    # ── Support / Resistance (simple: recent swing low/high) ──
    lookback = min(20, len(df))
    df["support"] = df["low"].rolling(lookback).min()
    df["resistance"] = df["high"].rolling(lookback).max()

    # ── Write to DB (only rows with valid RSI, i.e. skip first 13) ──
    valid = df.dropna(subset=["rsi_14"])

    # ONE round-trip, not one per row. The SQL this replaced batched its
    # writes; the port issued a separate upsert per row, so a single ticker's
    # 287 indicator rows became 287 round-trips — the 22.6s/ticker shape that
    # turns a universe repair into ~16 hours, and what TestWritesAreBatched
    # exists to pin.
    batch: list[dict] = []
    for _, row in valid.iterrows():
        doc = {
            "ticker": ticker.upper(),
            "date": row["date"],
            "rsi_14": _f(row["rsi_14"]),
            "macd": _f(row["macd"]),
            "macd_signal": _f(row["macd_signal"]),
            "macd_hist": _f(row["macd_hist"]),
            "sma_20": _f(row["sma_20"]),
            "sma_50": _f(row["sma_50"]),
            "sma_200": _f(row["sma_200"]),
            "ema_12": _f(row["ema_12"]),
            "ema_26": _f(row["ema_26"]),
            "bb_upper": _f(row["bb_upper"]),
            "bb_mid": _f(row["bb_mid"]),
            "bb_lower": _f(row["bb_lower"]),
            "atr_14": _f(row["atr_14"]),
            "adx_14": _f(row["adx_14"]),
            "stoch_k": _f(row["stoch_k"]),
            "stoch_d": _f(row["stoch_d"]),
            "obv": _f(row["obv"]),
            "vwap": _f(row["vwap"]),
            "support": _f(row["support"]),
            "resistance": _f(row["resistance"]),
        }
        batch.append(doc)

    count = mongo_store.bulk_upsert("technicals", batch, key_field=("ticker", "date"))

    logger.debug("[tech] %s: %d technical rows written", ticker, count)
    return count


def get_signals(ticker: str) -> str:
    """
    Get the latest technical signals as pre-formatted text for the LLM.
    Returns a human-readable summary string the agent can analyze.
    """
    tech_docs = mongo_store.find_docs(
        "technicals",
        {"ticker": ticker.upper()},
        sort=[("date", -1)],
        limit=1,
    )

    if not tech_docs:
        return f"No technical data available for {ticker}."

    data = tech_docs[0]

    # Build labeled signal text
    lines = [f"=== TECHNICAL ANALYSIS: {ticker} (as of {data.get('date')}) ==="]

    # RSI
    rsi = data.get("rsi_14")
    if rsi is not None:
        label = "OVERBOUGHT" if rsi > 70 else "OVERSOLD" if rsi < 30 else "NEUTRAL"
        lines.append(f"RSI-14: {rsi:.1f} ({label})")

    # MACD
    macd_h = data.get("macd_hist")
    if macd_h is not None:
        signal = "BULLISH" if macd_h > 0 else "BEARISH"
        lines.append(f"MACD histogram: {macd_h:.4f} ({signal})")

    price_docs = mongo_store.find_docs(
        "price_history",
        _one_vendor(ticker.upper(), {"ticker": ticker.upper()}),
        sort=[("date", -1)],
        limit=1,
    )
    close_price = price_docs[0].get("close") if price_docs else None
    if close_price is not None:
        price = float(close_price)
        lines.append(f"Price: ${price:.2f}")
        for ma_name in ["sma_20", "sma_50", "sma_200"]:
            val = data.get(ma_name)
            if val:
                pos = "ABOVE" if price > val else "BELOW"
                lines.append(f"  {ma_name.upper()}: ${val:.2f} (price {pos})")

    # Bollinger Bands
    bb_u, bb_l = data.get("bb_upper"), data.get("bb_lower")
    if bb_u and bb_l and close_price is not None:
        pct = (price - bb_l) / (bb_u - bb_l) * 100 if bb_u != bb_l else 50
        band_pos = (
            "UPPER BAND" if pct > 80 else "LOWER BAND" if pct < 20 else "MID RANGE"
        )
        lines.append(f"Bollinger: {band_pos} ({pct:.0f}% width)")

    # Stochastic
    k, d = data.get("stoch_k"), data.get("stoch_d")
    if k is not None:
        label = "OVERBOUGHT" if k > 80 else "OVERSOLD" if k < 20 else "NEUTRAL"
        lines.append(f"Stochastic K/D: {k:.1f}/{d:.1f} ({label})" if d is not None else f"Stochastic K: {k:.1f} ({label})")

    # ATR
    atr = data.get("atr_14")
    if atr is not None:
        lines.append(f"ATR-14: ${atr:.2f} (daily volatility)")

    # ADX
    adx = data.get("adx_14")
    if adx is not None:
        strength = "STRONG TREND" if adx > 25 else "WEAK/NO TREND"
        lines.append(f"ADX-14: {adx:.1f} ({strength})")

    # Support/Resistance
    sup, res = data.get("support"), data.get("resistance")
    if sup and res:
        lines.append(f"Support: ${sup:.2f} | Resistance: ${res:.2f}")

    return "\n".join(lines)


def _f(val) -> float | None:
    """Convert to float, handling NaN."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val)
