import logging
from datetime import datetime, timezone

import pandas as pd
import pymongo

from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


def get_sector_correlation_map(period: str):
    return mongo_query.find_dicts("sector_correlations", {"period": period})


def get_inverse_sector_pairs(period: str):
    return mongo_query.find_dicts(
        "sector_correlations",
        {"period": period, "correlation": {"$lt": -0.4}},
        sort=[("correlation", pymongo.ASCENDING)],
    )


def get_commodity_sector_links(commodity: str):
    return mongo_query.find_dicts(
        "stock_commodity_correlations",
        {"commodity": commodity,
         "$or": [{"correlation": {"$gt": 0.3}}, {"correlation": {"$lt": -0.3}}]},
        sort=[("correlation", pymongo.DESCENDING)],
    )


async def compute_all_correlations():
    logger.info("Computing all correlations...")
    inserts = []
    comm_inserts = []
    # 1. Sector-Pair Correlations
    cols = ["sector", "date", "avg_return_1d"]
    rows = mongo_query.find_rows(
        "sector_performance", {}, cols, sort=[("date", pymongo.ASCENDING)]
    )
    df_sector = pd.DataFrame(rows, columns=cols)

    if not df_sector.empty:
        df_sector["date"] = pd.to_datetime(df_sector["date"])
        pivot_sector = df_sector.pivot(
            index="date", columns="sector", values="avg_return_1d"
        )

        periods = {"30d": 30, "90d": 90}

        for period_name, days in periods.items():
            recent_data = pivot_sector.tail(days)
            if len(recent_data) < days * 0.5:
                continue

            corr_matrix = recent_data.corr()
            sectors = corr_matrix.columns

            for i in range(len(sectors)):
                for j in range(i + 1, len(sectors)):
                    sec_a = sectors[i]
                    sec_b = sectors[j]
                    corr = corr_matrix.loc[sec_a, sec_b]

                    if pd.isna(corr):
                        continue

                    tier = "neutral"
                    if corr > 0.8:
                        tier = "highly_correlated"
                    elif corr > 0.5:
                        tier = "correlated"
                    elif corr < -0.5:
                        tier = "inversely_correlated"
                    elif corr < -0.2:
                        tier = "weakly_inversely_correlated"

                    inserts.append(
                        (
                            sec_a,
                            sec_b,
                            float(corr),
                            tier,
                            period_name,
                            len(recent_data),
                        )
                    )

        for sec_a, sec_b, corr_v, tier_v, period_v, points in inserts:
            mongo_store.upsert_doc(
                "sector_correlations",
                {"sector_a": sec_a, "sector_b": sec_b, "period": period_v},
                {
                    "sector_a": sec_a,
                    "sector_b": sec_b,
                    "correlation": corr_v,
                    "tier": tier_v,
                    "period": period_v,
                    "data_points": points,
                    "computed_at": datetime.now(timezone.utc),
                },
            )

    # 2. Stock vs Commodity Correlations
    # price_history INNER JOIN ticker_metadata ON ticker, S&P 500 + yfinance.
    stock_cols = ["ticker", "date", "stock_price", "sector"]
    rows = mongo_query.join_rows(
        "price_history", {"source": "yfinance"}, "ticker",
        "ticker_metadata", "ticker", {"sp500": True},
        left_fields=["ticker", "date", "close"], right_fields=["sector"],
        select=[("l", "ticker"), ("l", "date"), ("l", "close"), ("r", "sector")],
    )
    df_stocks = pd.DataFrame(rows, columns=stock_cols)

    # `SELECT symbol AS commodity, date, close AS comm_price`: the aliases
    # become the DataFrame column names; the fields are read under their real
    # names.
    comm_cols = ["commodity", "date", "comm_price"]
    rows = mongo_query.find_rows(
        "asset_prices", {"asset_class": "commodity"}, ["symbol", "date", "close"]
    )
    df_comms = pd.DataFrame(rows, columns=comm_cols)

    if df_stocks.empty or df_comms.empty:
        logger.warning(
            "Missing stock or commodity data. Skipping commodity correlations."
        )
    else:
        df_stocks["date"] = pd.to_datetime(df_stocks["date"])
        df_comms["date"] = pd.to_datetime(df_comms["date"])

        pivot_stocks = df_stocks.pivot(
            index="date", columns="ticker", values="stock_price"
        ).pct_change()
        pivot_comms = df_comms.pivot(
            index="date", columns="commodity", values="comm_price"
        ).pct_change()

        joined = pivot_stocks.join(pivot_comms, how="inner")

        periods = {"30d": 30, "90d": 90}

        for period_name, days in periods.items():
            recent_data = joined.tail(days)
            if len(recent_data) < days * 0.5:
                continue

            tickers = pivot_stocks.columns
            commodities = pivot_comms.columns

            for comm in commodities:
                if comm not in recent_data.columns:
                    continue
                for ticker in tickers:
                    if ticker not in recent_data.columns:
                        continue

                    valid = recent_data[[ticker, comm]].dropna()
                    if len(valid) < 10:
                        continue

                    corr = valid[ticker].corr(valid[comm])
                    if pd.isna(corr):
                        continue

                    sensitivity = "neutral"
                    if corr > 0.4:
                        sensitivity = "highly_sensitive"
                    elif corr < -0.4:
                        sensitivity = "inversely_sensitive"

                    # Store all calculated correlations
                    comm_inserts.append(
                        (
                            ticker,
                            comm,
                            float(corr),
                            sensitivity,
                            period_name,
                            len(valid),
                        )
                    )

        for tkr, comm_s, corr_v, sens, period_v, points in comm_inserts:
            mongo_store.upsert_doc(
                "stock_commodity_correlations",
                {"ticker": tkr, "commodity": comm_s, "period": period_v},
                {
                    "ticker": tkr,
                    "commodity": comm_s,
                    "correlation": corr_v,
                    "sensitivity": sens,
                    "period": period_v,
                    "data_points": points,
                    "computed_at": datetime.now(timezone.utc),
                },
            )

    logger.info(
        f"Computed {len(inserts)} sector correlations and {len(comm_inserts)} commodity correlations."
    )
    return f"Computed {len(inserts)} sector & {len(comm_inserts)} comm correlations"
