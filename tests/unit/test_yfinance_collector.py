"""yfinance collector tests, ported off the inert `get_db` mock.

The old fixture patched `get_db` on `yfinance_collector`, `news_collector` and
`processors.dedup_engine`, then asserted on `db.executemany` / `db.execute`.
After the Postgres->Mongo migration `yfinance_collector` and `dedup_engine`
no longer import `get_db` at all — they write through
`mongo_store.upsert_doc` and read through `mongo_store`/`mongo_query` — so the
patch intercepted nothing and every price bar, fundamentals row and duplicate
check went to the LIVE Mongo database. This file patches `mongo_store` on
`yfinance_collector` and `dedup_engine`, plus `mongo_query`/`mongo_store` on
`news_collector` (which `collect_news` proxies to); `news_collector` no longer
imports `get_db` at all, so that mock is gone
only for the one Postgres read `news_collector` still performs. The
`executemany`-based assertions became per-document `upsert_doc` assertions on
the `price_history` collection — the written dates and bar counts are checked
directly rather than through an opaque row tuple.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import datetime
import pandas as pd

from app.collectors.yfinance_collector import (
    collect_price_history,
    collect_fundamentals,
    collect_financials,
    collect_balance_sheet,
    collect_news,
    collect_all,
)


class _Mongo:
    """Handles on every Mongo surface the code under test can reach."""

    def __init__(self, yf_store, news_store, news_query, dedup_store, db):
        self.yf_store = yf_store
        self.news_store = news_store
        self.news_query = news_query
        self.dedup_store = dedup_store
        self.db = db

    def upserts(self, collection, store=None):
        """(key, doc) pairs upserted into `collection`."""
        store = store or self.yf_store
        return [
            (c[0][1], c[0][2])
            for c in store.upsert_doc.call_args_list
            if c[0][0] == collection
        ]


@pytest.fixture
def mongo():
    db = MagicMock()
    db.fetchone.return_value = None
    db.fetchall.return_value = []
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    db.execute.return_value = cursor

    with patch("app.collectors.yfinance_collector.mongo_store") as yf_store, \
         patch("app.collectors.news_collector.mongo_store") as news_store, \
         patch("app.collectors.news_collector.mongo_query") as news_query, \
         patch("app.processors.dedup_engine.mongo_store") as dedup_store:

        # Nothing on record => nothing is a duplicate, no publisher is banned.
        dedup_store.count_docs.return_value = 0
        dedup_store.find_docs.return_value = []
        # find_rows returns TUPLES: (source_name, win_rate, total_items).
        news_query.find_rows.return_value = []
        # agg_row backs url_fanout_exceeded; None => under the cap.
        news_query.agg_row.return_value = None
        # news_articles must be treated as a Mongo-backed table so the write
        # path under test is the one that actually runs in production.
        news_store.writes_mongo.return_value = True
        news_store.writes_pg.return_value = False

        yield _Mongo(yf_store, news_store, news_query, dedup_store, db)


@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_collect_price_history_success(mock_ticker, mongo):

    # Mock DataFrame
    df = pd.DataFrame({
        "Open": [100.0, 101.0],
        "High": [105.0, 106.0],
        "Low": [95.0, 96.0],
        "Close": [102.0, 103.0],
        "Volume": [1000, 2000]
    }, index=pd.to_datetime(["2023-01-01", "2023-01-02"]))

    mock_ticker_inst = MagicMock()
    mock_ticker_inst.history.return_value = df
    mock_ticker.return_value = mock_ticker_inst

    count = await collect_price_history("AAPL")

    assert count == 2
    writes = mongo.upserts("price_history")
    assert len(writes) == 2
    assert [k["date"] for k, _ in writes] == [
        datetime.date(2023, 1, 1),
        datetime.date(2023, 1, 2),
    ]
    assert all(k["ticker"] == "AAPL" and k["source"] == "yfinance" for k, _ in writes)

@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_collect_price_history_salvages_frame_with_one_nan_bar(mock_ticker, mongo):
    """One incomplete bar must not discard the complete ones.

    Reproduces the 2026-07-26 outage shape exactly: yfinance returns the newest
    session with NaN OHLC and a non-null Volume. Before the salvage this frame
    failed PriceHistorySchema ("non-nullable series 'Open' contains null
    values") and returned 0, so all 12 tickers in that cycle fell back to
    cached prices while reporting success.
    """
    df = pd.DataFrame({
        "Open": [100.0, 101.0, float("nan")],
        "High": [105.0, 106.0, float("nan")],
        "Low": [95.0, 96.0, float("nan")],
        "Close": [102.0, 103.0, float("nan")],
        "Volume": [1000, 2000, 2582031],
    }, index=pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03"]))

    mock_ticker_inst = MagicMock()
    mock_ticker_inst.history.return_value = df
    mock_ticker.return_value = mock_ticker_inst

    count = await collect_price_history("AAPL")

    # The two complete bars are kept; only the incomplete one is dropped.
    assert count == 2
    writes = mongo.upserts("price_history")
    assert len(writes) == 2
    assert datetime.date(2023, 1, 3) not in [k["date"] for k, _ in writes]


@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_collect_price_history_salvages_inconsistent_bar(mock_ticker, mongo):
    """One internally inconsistent bar must not discard the frame — repeatedly.

    Reproduces cycle-v3-1785504601: yfinance shipped RBLX's 2026-07-18
    gap-down session with Open=49.46 above High=40.0. The frame-level
    high_is_max check rejected all 125 rows, and because the bar stayed inside
    the 6-month window, every later fetch failed identically — 10 straight
    sessions with no yfinance writes, and a desk that priced RBLX 24% off.
    """
    df = pd.DataFrame({
        "Open": [100.0, 49.46, 101.0],
        "High": [105.0, 40.0, 106.0],
        "Low": [95.0, 38.93, 96.0],
        "Close": [102.0, 39.11, 103.0],
        "Volume": [1000, 3132086, 2000],
    }, index=pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03"]))

    mock_ticker_inst = MagicMock()
    mock_ticker_inst.history.return_value = df
    mock_ticker.return_value = mock_ticker_inst

    count = await collect_price_history("RBLX")

    # The two consistent bars are kept; only the impossible one is dropped.
    assert count == 2
    writes = mongo.upserts("price_history")
    assert len(writes) == 2
    written_dates = [k["date"] for k, _ in writes]
    assert datetime.date(2023, 1, 2) not in written_dates


@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_collect_price_history_all_bars_inconsistent(mock_ticker, mongo):
    """All-bad frame still reports 0 — salvage must not manufacture success."""
    df = pd.DataFrame({
        "Open": [49.46],
        "High": [40.0],
        "Low": [38.93],
        "Close": [39.11],
        "Volume": [3132086],
    }, index=pd.to_datetime(["2023-01-02"]))

    mock_ticker_inst = MagicMock()
    mock_ticker_inst.history.return_value = df
    mock_ticker.return_value = mock_ticker_inst

    count = await collect_price_history("RBLX")

    assert count == 0
    assert mongo.upserts("price_history") == []


@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_collect_price_history_all_bars_incomplete(mock_ticker, mongo):
    """Salvage must not manufacture success when nothing is usable."""
    df = pd.DataFrame({
        "Open": [float("nan")],
        "High": [float("nan")],
        "Low": [float("nan")],
        "Close": [float("nan")],
        "Volume": [123],
    }, index=pd.to_datetime(["2023-01-03"]))

    mock_ticker_inst = MagicMock()
    mock_ticker_inst.history.return_value = df
    mock_ticker.return_value = mock_ticker_inst

    count = await collect_price_history("AAPL")

    assert count == 0
    assert mongo.upserts("price_history") == []


@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_collect_price_history_empty(mock_ticker, mongo):
    mock_ticker_inst = MagicMock()
    mock_ticker_inst.history.return_value = pd.DataFrame()
    mock_ticker.return_value = mock_ticker_inst

    count = await collect_price_history("AAPL")
    assert count == 0

@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_collect_fundamentals_success(mock_ticker, mongo):
    mock_ticker_inst = MagicMock()
    mock_ticker_inst.info = {"symbol": "AAPL", "marketCap": 1000000}
    mock_ticker.return_value = mock_ticker_inst

    result = await collect_fundamentals("AAPL")

    assert result is True
    writes = mongo.upserts("fundamentals")
    assert len(writes) == 1
    key, doc = writes[0]
    assert key["ticker"] == "AAPL"
    assert doc["source"] == "yfinance"
    assert doc["market_cap"] == 1000000

@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_collect_fundamentals_missing_data(mock_ticker, mongo):
    mock_ticker_inst = MagicMock()
    mock_ticker_inst.info = {}
    mock_ticker.return_value = mock_ticker_inst

    result = await collect_fundamentals("AAPL")
    assert result is False
    # A failed fetch must not write a hollow fundamentals row.
    assert mongo.upserts("fundamentals") == []

@pytest.mark.asyncio
@patch("app.collectors.news_collector._scrape_article_body_via_service", new_callable=AsyncMock)
@patch("yfinance.Ticker")
async def test_collect_news_success(mock_ticker, mock_scrape, mongo):

    # No publisher is on the bad list.
    mongo.news_query.find_rows.return_value = []
    mock_scrape.return_value = "A" * 200

    mock_ticker_inst = MagicMock()
    mock_ticker_inst.news = [
        {"content": {"title": "Test 1 Article Headline that is long enough to pass quality gate", "canonicalUrl": {"url": "http://1"}, "provider": {"displayName": "Provider 1"}, "pubDate": "2023-10-01T12:00:00Z", "description": "A" * 200}},
        {"content": {"title": "Test 2 Article Headline that is long enough to pass quality gate", "clickThroughUrl": {"url": "http://2"}, "description": "A" * 200}},
    ]
    mock_ticker.return_value = mock_ticker_inst

    count = await collect_news("AAPL")

    assert count == 2
    writes = mongo.upserts("news_articles", store=mongo.news_store)
    assert len(writes) == 2
    assert all(doc["source"] == "yfinance" for _k, doc in writes)

@pytest.mark.asyncio
@patch("app.collectors.news_collector._scrape_article_body_via_service", new_callable=AsyncMock)
@patch("yfinance.Ticker")
async def test_collect_news_bad_publisher(mock_ticker, mock_scrape, mongo):

    # Bad publishers come from source_trust as TUPLES
    # (source_name, win_rate, total_items); win_rate < 0.1 and total_items >= 5
    # is what makes one "bad".
    mongo.news_query.find_rows.return_value = [("Bad Provider", 0.0, 10)]
    mock_scrape.return_value = "A" * 200

    mock_ticker_inst = MagicMock()
    mock_ticker_inst.news = [
        {"content": {"title": "Test 1 Article Headline that is long enough to pass quality gate", "canonicalUrl": {"url": "http://1"}, "provider": {"displayName": "Bad Provider"}, "description": "A" * 200}},
        {"content": {"title": "Test 2 Article Headline that is long enough to pass quality gate", "canonicalUrl": {"url": "http://2"}, "provider": {"displayName": "Good Provider"}, "description": "A" * 200}},
    ]
    mock_ticker.return_value = mock_ticker_inst

    count = await collect_news("AAPL")

    # Should only insert the good provider
    assert count == 1
    writes = mongo.upserts("news_articles", store=mongo.news_store)
    assert [doc["publisher"] for _k, doc in writes] == ["Good Provider"]

@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.collect_balance_sheet")
@patch("app.collectors.yfinance_collector.collect_financials")
@patch("app.collectors.yfinance_collector.collect_fundamentals")
@patch("app.collectors.yfinance_collector.collect_price_history")
async def test_collect_all(mock_price, mock_fundamentals, mock_financials, mock_balance):
    mock_price.return_value = 10
    mock_fundamentals.return_value = True
    mock_financials.return_value = 4
    mock_balance.return_value = 2

    result = await collect_all("AAPL")

    assert result["ticker"] == "AAPL"
    assert result["price_rows"] == 10
    assert result["fundamentals"] is True
    assert result["financial_rows"] == 4
    assert result["balance_rows"] == 2
