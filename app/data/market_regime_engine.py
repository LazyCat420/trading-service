import asyncio
import logging
from datetime import datetime, timezone

import pandas as pd
import pymongo
import yfinance as yf
from app.db import date_fields, mongo_query, mongo_store

logger = logging.getLogger(__name__)


def get_latest_regime():
    rows = mongo_query.find_dicts(
        "market_regime", {}, sort=[("date", pymongo.DESCENDING)], limit=1
    )
    return rows[0] if rows else None


def get_sector_breadth_data():
    # `WHERE date = (SELECT MAX(date) ...)` — the scalar subquery becomes a
    # separate MAX aggregate, then an equality match on the value it returned.
    latest = mongo_query.agg_row("sector_breadth", {}, [("max", "date")])[0]
    if latest is None:
        return []
    return mongo_query.find_dicts("sector_breadth", {"date": latest})


def get_cross_correlations(period: str):
    return mongo_query.find_dicts("cross_asset_correlations", {"period": period})


async def detect_anomalies():
    try:
        return mongo_query.find_dicts(
            "anomalies", {}, sort=[("detected_at", pymongo.DESCENDING)], limit=10
        )
    except Exception:
        return []


async def compute_sector_breadth():
    logger.info("Computing sector breadth...")
    # price_history JOIN ticker_metadata ON ticker, filtered to S&P 500
    # yfinance rows, ordered by date. INNER join: a price row whose ticker
    # has no metadata is dropped, same as the SQL.
    cols = ["ticker", "date", "close", "sector"]
    rows = mongo_query.join_rows(
        "price_history", {"source": "yfinance"}, "ticker",
        "ticker_metadata", "ticker", {"sp500": True},
        left_fields=["ticker", "date", "close"], right_fields=["sector"],
        select=[("l", "ticker"), ("l", "date"), ("l", "close"), ("r", "sector")],
        sort=[("date", pymongo.ASCENDING)],
    )
    import pandas as pd

    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return

    df["date"] = pd.to_datetime(df["date"])

    df["sma50"] = df.groupby("ticker")["close"].transform(
        lambda x: x.rolling(50).mean()
    )
    df["sma200"] = df.groupby("ticker")["close"].transform(
        lambda x: x.rolling(200).mean()
    )
    df["high_252"] = df.groupby("ticker")["close"].transform(
        lambda x: x.rolling(252).max()
    )
    df["low_252"] = df.groupby("ticker")["close"].transform(
        lambda x: x.rolling(252).min()
    )

    df["above_50"] = df["close"] > df["sma50"]
    df["above_200"] = df["close"] > df["sma200"]
    df["is_new_high"] = df["close"] >= df["high_252"]
    df["is_new_low"] = df["close"] <= df["low_252"]

    latest_date = df["date"].max()
    latest_df = df[df["date"] == latest_date]

    sectors = latest_df["sector"].unique()
    inserts = []

    for sector in sectors:
        sdf = latest_df[latest_df["sector"] == sector]
        if sdf.empty:
            continue

        pct_above_50 = sdf["above_50"].mean() * 100
        pct_above_200 = sdf["above_200"].mean() * 100
        new_highs = sdf["is_new_high"].sum()
        new_lows = sdf["is_new_low"].sum()
        net_highs = new_highs - new_lows

        inserts.append(
            (
                sector,
                date_fields.as_date(latest_date),   # DATE column — see date_fields
                float(pct_above_50) if pd.notna(pct_above_50) else 0.0,
                float(pct_above_200) if pd.notna(pct_above_200) else 0.0,
                int(new_highs),
                int(new_lows),
                int(net_highs),
            )
        )

    for item in inserts:
        sector, date_s, pct50, pct200, nh, nl, net = item
        mongo_store.upsert_doc(
            "sector_breadth",
            {"sector": sector, "date": date_s},
            {
                "sector": sector,
                "date": date_s,
                "pct_above_sma50": pct50,
                "pct_above_sma200": pct200,
                "new_highs": nh,
                "new_lows": nl,
                "net_highs": net,
                "computed_at": datetime.now(timezone.utc),
            },
        )


async def compute_market_regime():
    logger.info("Computing market regime...")

    tickers = ["^VIX", "^TNX", "DX-Y.NYB", "SPY"]
    try:
        # Off the event loop — same class of bug as the S&P 500 bulk collector
        # (2026-07-27): synchronous network I/O inside an async function that
        # boot_service awaits, so the HTTP server sharing the loop stops
        # answering /health. Only 4 tickers here, so the stall is shorter than
        # the ~45s bulk one, but it lands in the same startup window.
        data = (await asyncio.to_thread(
            yf.download, tickers, period="1mo", progress=False
        ))["Close"]
        if data.empty:
            return

        latest = data.iloc[-1]
        prev_5d = data.iloc[-5] if len(data) >= 5 else data.iloc[0]

        vix_level = float(latest.get("^VIX", 15.0))
        yield_10y = float(latest.get("^TNX", 4.0))
        dollar_index = float(latest.get("DX-Y.NYB", 100.0))
        sp500_level = float(latest.get("SPY", 5000.0))

        prev_spy = float(prev_5d.get("SPY", sp500_level))
        prev_dollar = float(prev_5d.get("DX-Y.NYB", dollar_index))

        sp500_change_5d = (
            ((sp500_level - prev_spy) / prev_spy) * 100 if prev_spy > 0 else 0.0
        )
        dollar_change_5d = (
            ((dollar_index - prev_dollar) / prev_dollar) * 100
            if prev_dollar > 0
            else 0.0
        )

        if vix_level > 25:
            regime_label = "Crisis"
        elif vix_level > 20:
            regime_label = "Risk-Off"
        elif sp500_change_5d > 1.0:
            regime_label = "Risk-On"
        else:
            regime_label = "Neutral"

        doc = {
            "date": date_fields.as_date(pd.Timestamp.now()),   # DATE column
            "vix_level": vix_level,
            "vix_signal": "Elevated" if vix_level > 20 else "Normal",
            "vix_zscore": 0.0,
            "vix_term_ratio": 1.0,
            "vix_term_signal": "Normal",
            "yield_2y": yield_10y,
            "yield_10y": yield_10y,
            "yield_2y10y_spread": 0.0,
            "yield_signal": "Normal",
            "dollar_index": dollar_index,
            "dollar_change_5d": dollar_change_5d,
            "sp500_level": sp500_level,
            "sp500_change_5d": sp500_change_5d,
            "breadth_sp500": 50.0,
            "regime_label": regime_label,
            "computed_at": datetime.now(timezone.utc),
        }
        mongo_store.upsert_doc("market_regime", {"date": doc["date"]}, doc)

    except Exception as e:
        logger.error(f"Error computing market regime: {e}")


async def compute_cross_asset_correlations():
    logger.info("Computing cross asset correlations...")
    # Safe fallback if not fully implemented yet
    pass
