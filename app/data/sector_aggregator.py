import logging
from datetime import datetime, timezone

import pymongo

from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


def get_sector_heatmap():
    row = mongo_query.agg_row('sector_performance', {}, [('max', 'date')])
    if not row or not row[0]:
        return []

    latest_date = row[0]
    return mongo_query.find_dicts("sector_performance", {"date": latest_date})


def get_sector_stocks(sector: str):
    """The SQL's two LEFT JOINs, done as two keyed reads plus a Python stitch.

    mongo_query.join_rows is INNER-only by design, and an inner join here would
    silently drop every S&P 500 name that has no price row — turning a sector
    listing with holes in it into a shorter listing that looks complete.
    """
    max_date = mongo_query.agg_row("price_history", {}, [("max", "date")])[0]
    if max_date is None:
        return []

    # `(l.max_date - INTERVAL '1 day')::date` is the previous CALENDAR day, not
    # the previous trading day, so return_1d is 0 for every stock whenever the
    # latest bar is a Monday. Preserved exactly — this port must not change the
    # numbers.
    prev_date = _minus_one_day(max_date)

    # price_history's PK is (ticker, date, source) and the vendors disagree by
    # ~20% on adjusted closes, so a multi-ticker read must pin ONE vendor per
    # ticker — otherwise a dict keyed on ticker keeps whichever vendor's row
    # happened to come last, and return_1d can compare two different vendors.
    # The SQL this replaces was the guard's budgeted unpinned read in this file.
    latest = _prices_on(max_date, ["ticker", "close", "volume"])
    prev = {t: v[0] for t, v in _prices_on(prev_date, ["ticker", "close"]).items()}

    out = []
    for t in mongo_query.find_rows(
        "ticker_metadata", {"sector": sector, "sp500": True},
        ["ticker", "name", "market_cap", "industry"],
    ):
        ticker, name, market_cap, industry = t
        price, volume = latest.get(ticker, (None, None))
        prev_close = prev.get(ticker)
        # CASE WHEN p.prev_close > 0 THEN ... ELSE 0 — a NULL prev_close makes
        # the predicate NULL, which is not true, so SQL took the ELSE branch.
        if prev_close is not None and prev_close > 0 and price is not None:
            return_1d = ((price - prev_close) / prev_close) * 100
        else:
            return_1d = 0
        out.append({
            "ticker": ticker, "name": name, "market_cap": market_cap,
            "industry": industry, "price": price, "volume": volume,
            "return_1d": return_1d,
        })

    # ORDER BY t.market_cap DESC NULLS LAST
    out.sort(key=lambda r: (r["market_cap"] is None, -(r["market_cap"] or 0)))
    return out


def _prices_on(on_date, columns):
    """`{ticker: (columns[1:]...)}` for one date, with one vendor per ticker.

    Reads `source` so keep_dominant_source() can pick the ticker's dominant
    vendor, then drops it — the same contract the SQL callers use.
    """
    import pandas as pd

    from app.quant.returns import keep_dominant_source

    rows = mongo_query.find_rows(
        "price_history", {"date": on_date}, list(columns) + ["source"]
    )
    frame = pd.DataFrame(rows, columns=list(columns) + ["source"])
    if frame.empty:
        return {}
    frame = keep_dominant_source(frame)
    return {
        r[0]: tuple(r[1:])
        for r in frame[list(columns)].itertuples(index=False, name=None)
    }


def _minus_one_day(value):
    """`(d - INTERVAL '1 day')::date` for whichever type price_history.date holds
    (a date/datetime, or the 'YYYY-MM-DD' string the writers in this package
    store)."""
    from datetime import date, datetime, timedelta

    if isinstance(value, str):
        return (datetime.strptime(value[:10], "%Y-%m-%d").date()
                - timedelta(days=1)).strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value - timedelta(days=1)
    if isinstance(value, date):
        return value - timedelta(days=1)
    return value


async def compute_sector_performance():
    logger.info("Computing sector performance...")
    # Load all price history and metadata into pandas for vectorized operations
    # price_history INNER JOIN ticker_metadata ON ticker, S&P 500 members
    # with a non-null sector.
    cols = ["ticker", "date", "close", "volume", "sector", "market_cap"]
    rows = mongo_query.join_rows(
        "price_history", {}, "ticker",
        "ticker_metadata", "ticker",
        {"sp500": True, "sector": {"$ne": None}},
        left_fields=["ticker", "date", "close", "volume"],
        right_fields=["sector", "market_cap"],
        select=[("l", "ticker"), ("l", "date"), ("l", "close"),
                ("l", "volume"), ("r", "sector"), ("r", "market_cap")],
        sort=[("date", pymongo.ASCENDING)],
    )
    import pandas as pd

    df = pd.DataFrame(rows, columns=cols)

    if df.empty:
        logger.warning("No price data found. Cannot compute sector performance.")
        return "No data"

    # Convert date to datetime
    df["date"] = pd.to_datetime(df["date"])

    # Calculate daily returns per ticker
    df["return_1d"] = df.groupby("ticker")["close"].pct_change()
    df["return_5d"] = df.groupby("ticker")["close"].pct_change(periods=5)
    df["return_30d"] = df.groupby("ticker")["close"].pct_change(periods=30)

    # For volume, maybe calculate ratio compared to 30d SMA
    df["vol_sma_30"] = df.groupby("ticker")["volume"].transform(
        lambda x: x.rolling(30).mean()
    )
    df["vol_ratio"] = df["volume"] / df["vol_sma_30"]

    # We only care about the latest date for each sector
    latest_date = df["date"].max()
    latest_df = df[df["date"] == latest_date].copy()

    if latest_df.empty:
        return "No recent data"

    sectors = latest_df["sector"].unique()
    inserts = []

    for sector in sectors:
        sdf = latest_df[latest_df["sector"] == sector]

        # Calculate market cap weights
        total_mcap = sdf["market_cap"].sum()
        if total_mcap > 0:
            weights = sdf["market_cap"] / total_mcap
            avg_1d = (sdf["return_1d"] * weights).sum() * 100
            avg_5d = (sdf["return_5d"] * weights).sum() * 100
            avg_30d = (sdf["return_30d"] * weights).sum() * 100
        else:
            avg_1d = sdf["return_1d"].mean() * 100
            avg_5d = sdf["return_5d"].mean() * 100
            avg_30d = sdf["return_30d"].mean() * 100

        # Breadth (pct of stocks positive over 1d)
        breadth_pct = (sdf["return_1d"] > 0).mean() * 100

        # Top gainer / loser (1d)
        if not sdf.empty and not sdf["return_1d"].isna().all():
            top_gainer_row = sdf.loc[sdf["return_1d"].idxmax()]
            top_loser_row = sdf.loc[sdf["return_1d"].idxmin()]
            top_gainer = top_gainer_row["ticker"]
            top_gainer_return = top_gainer_row["return_1d"] * 100
            top_loser = top_loser_row["ticker"]
            top_loser_return = top_loser_row["return_1d"] * 100
        else:
            top_gainer, top_gainer_return, top_loser, top_loser_return = (
                None,
                0,
                None,
                0,
            )

        avg_volume_ratio = sdf["vol_ratio"].mean()
        stock_count = len(sdf)

        # Momentum signal based on 5d
        momentum_signal = (
            "Bullish"
            if avg_5d > 1.0
            else ("Bearish" if avg_5d < -1.0 else "Neutral")
        )

        inserts.append(
            (
                sector,
                latest_date.strftime("%Y-%m-%d"),
                float(avg_1d) if pd.notna(avg_1d) else 0.0,
                float(avg_5d) if pd.notna(avg_5d) else 0.0,
                float(avg_30d) if pd.notna(avg_30d) else 0.0,
                float(breadth_pct) if pd.notna(breadth_pct) else 0.0,
                top_gainer,
                float(top_gainer_return) if pd.notna(top_gainer_return) else 0.0,
                top_loser,
                float(top_loser_return) if pd.notna(top_loser_return) else 0.0,
                float(avg_volume_ratio) if pd.notna(avg_volume_ratio) else 1.0,
                momentum_signal,
                stock_count,
            )
        )

    _PERF_FIELDS = (
        "sector", "date", "avg_return_1d", "avg_return_5d", "avg_return_30d",
        "breadth_pct", "top_gainer", "top_gainer_return", "top_loser",
        "top_loser_return", "avg_volume_ratio", "momentum_signal",
        "stock_count",
    )
    for item in inserts:
        doc = dict(zip(_PERF_FIELDS, item))
        doc["computed_at"] = datetime.now(timezone.utc)
        mongo_store.upsert_doc(
            "sector_performance",
            {"sector": doc["sector"], "date": doc["date"]},
            doc,
        )

    logger.info(f"Computed and saved performance for {len(inserts)} sectors.")
    return f"Processed {len(inserts)} sectors"


async def backfill_sector_performance():
    """
    Derives daily sector avg_return_1d from price_history + ticker_metadata
    and writes to sector_performance. Only calculates historical 1-day returns
    to quickly backfill empty history needed by the correlation engine.
    """
    logger.info("Backfilling sector_performance from historical price_history...")
    # Check if we already have sufficient history (e.g. more than 1 day)
    row = mongo_query.agg_row('sector_performance', {}, [('count_distinct', 'date')])
    if row and row[0] > 1:
        logger.info("Sector performance already has history. Skipping backfill.")
        return

    cols = ["ticker", "date", "close", "sector"]
    rows = mongo_query.join_rows(
        "price_history", {"source": "yfinance"}, "ticker",
        "ticker_metadata", "ticker", {"sp500": True, "sector": {"$ne": None}},
        left_fields=["ticker", "date", "close"], right_fields=["sector"],
        select=[("l", "ticker"), ("l", "date"), ("l", "close"), ("r", "sector")],
        sort=[("ticker", pymongo.ASCENDING), ("date", pymongo.ASCENDING)],
    )

    if not rows:
        logger.warning("backfill_sector_performance: no price_history rows found, skipping.")
        return

    import pandas as pd
    df = pd.DataFrame(rows, columns=cols)

    if df.empty:
        logger.warning("backfill_sector_performance: no price_history rows found, skipping.")
        return

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"])

    # Compute 1-day % return per ticker
    df["return_1d"] = df.groupby("ticker")["close"].pct_change()

    # Aggregate to daily sector average return
    sector_daily = (
        df.groupby(["sector", "date"])["return_1d"]
        .mean()
        .reset_index()
        .rename(columns={"return_1d": "avg_return_1d"})
    )
    sector_daily = sector_daily.dropna(subset=["avg_return_1d"])

    inserts = [
        (row["sector"], row["date"].strftime("%Y-%m-%d"), float(row["avg_return_1d"]))
        for _, row in sector_daily.iterrows()
    ]

    if not inserts:
        logger.warning("backfill_sector_performance: computed 0 rows, check price_history data.")
        return

    for sector, date_s, avg_1d in inserts:
        mongo_store.upsert_doc(
            "sector_performance",
            {"sector": sector, "date": date_s},
            {
                "sector": sector,
                "date": date_s,
                "avg_return_1d": avg_1d,
                "computed_at": datetime.now(timezone.utc),
            },
        )

    logger.info(
        "backfill_sector_performance: inserted %d sector-day rows across %d sectors.",
        len(inserts),
        sector_daily["sector"].nunique(),
    )
