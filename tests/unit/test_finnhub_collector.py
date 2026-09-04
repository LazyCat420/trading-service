import pytest
from unittest.mock import patch, MagicMock
import datetime

from app.collectors.finnhub_collector import (
    collect_news,
    collect_analyst_targets,
    collect_earnings_calendar,
    collect_recommendation_trends,
    collect_all,
)


@pytest.fixture
def mock_db():
    """Isolate the collector's store.

    Was `patch("app.collectors.finnhub_collector.get_db")`, a symbol the
    module no longer has, so this raised AttributeError at SETUP and all 11
    tests in the file ERRORED rather than failed — invisible in a summary line
    that only counts failures.

    The collector writes through `mongo_store.upsert_doc` now; the returned
    mock is that module, so assertions read the collection, key and document.
    """
    store = MagicMock()
    store.writes_mongo.return_value = True
    store.writes_pg.return_value = False
    with patch("app.collectors.finnhub_collector.mongo_store", store):
        yield store


@pytest.fixture
def mock_client():
    client = MagicMock()
    return client


@pytest.mark.asyncio
@patch("app.collectors.news_collector.collect_finnhub_news")
async def test_collect_news_success(mock_collect_finnhub, mock_db):
    mock_collect_finnhub.return_value = 1
    
    count = await collect_news("AAPL", days_back=7)
    
    assert count == 1
    mock_collect_finnhub.assert_called_once_with("AAPL", days=7)

@pytest.mark.asyncio
@patch("app.collectors.news_collector.collect_finnhub_news")
async def test_collect_news_api_error(mock_collect_finnhub, mock_db):
    mock_collect_finnhub.side_effect = Exception("API rate limit")
    
    # Should handle error gracefully and return 0
    count = await collect_news("AAPL")
    assert count == 0


@pytest.mark.asyncio
@patch("app.collectors.finnhub_collector._get_client")
async def test_collect_analyst_targets_success(mock_get_client, mock_client):
    mock_get_client.return_value = mock_client
    mock_client.price_target.return_value = {"targetHigh": 200, "targetLow": 100, "targetMean": 150}
    
    result = await collect_analyst_targets("AAPL")
    assert result is True


@pytest.mark.asyncio
@patch("app.collectors.finnhub_collector._get_client")
async def test_collect_analyst_targets_no_data(mock_get_client, mock_client):
    mock_get_client.return_value = mock_client
    mock_client.price_target.return_value = {}  # Missing targetHigh
    
    result = await collect_analyst_targets("AAPL")
    assert result is False


@pytest.mark.asyncio
@patch("app.collectors.finnhub_collector._get_client")
async def test_collect_earnings_calendar(mock_get_client, mock_client):
    mock_get_client.return_value = mock_client
    mock_client.earnings_calendar.return_value = {"earningsCalendar": [{"date": "2023-11-01"}]}
    
    events = await collect_earnings_calendar("AAPL")
    assert len(events) == 1
    assert events[0]["date"] == "2023-11-01"


@pytest.mark.asyncio
@patch("app.collectors.finnhub_collector._get_client")
async def test_collect_recommendation_trends(mock_get_client, mock_client):
    mock_get_client.return_value = mock_client
    mock_client.recommendation_trends.return_value = [{"buy": 10, "hold": 5, "sell": 1}]
    
    trends = await collect_recommendation_trends("AAPL")
    assert len(trends) == 1
    assert trends[0]["buy"] == 10


@pytest.mark.asyncio
@patch("app.collectors.finnhub_collector.collect_recommendation_trends")
@patch("app.collectors.finnhub_collector.collect_earnings_calendar")
@patch("app.collectors.finnhub_collector.collect_analyst_targets")
@patch("app.collectors.finnhub_collector.collect_news")
async def test_collect_all(mock_news, mock_targets, mock_earnings, mock_trends):
    mock_news.return_value = 5
    mock_targets.return_value = True
    mock_earnings.return_value = [1, 2]
    mock_trends.return_value = [1]
    
    result = await collect_all("AAPL")
    assert result["ticker"] == "AAPL"
    assert result["news_articles"] == 5
    assert result["analyst_targets"] is True
    assert result["earnings_events"] == 2
    assert result["recommendation_snapshots"] == 1


# ── The supplement must never be the only row ───────────────────────────────
#
# `_merge_into_fundamentals` keyed on TODAY's date, so on any day the full
# snapshot collector had not run — every fast-path cycle skips it — an
# earnings-date lookup CREATED a four-field row for today. Every reader takes
# the newest row, so that stub then hid the previous day's 41-field snapshot
# and reported all 23 verified ratios as NOT ON FILE. Measured 2026-09-03:
# DELL's stub was written mid-cycle at 19:07:27 by the fundamental analyst's
# own get_upcoming_events call, and the debate that followed argued about
# "16 data gaps" against a full snapshot sitting one document away.
import datetime as _dt

from app.collectors.finnhub_collector import _merge_into_fundamentals


def test_merge_writes_into_the_newest_existing_row(mock_db):
    mock_db.find_docs.return_value = [
        {"ticker": "DELL", "snapshot_date": _dt.date(2026, 9, 2)}
    ]
    _merge_into_fundamentals("DELL", {"earnings_date": _dt.date(2026, 11, 23)})

    mock_db.upsert_doc.assert_called_once()
    _, key, doc = mock_db.upsert_doc.call_args[0]
    assert key == {"ticker": "DELL", "snapshot_date": _dt.date(2026, 9, 2)}, (
        "keying on today creates a stub row that hides the real snapshot"
    )
    assert doc == {"earnings_date": _dt.date(2026, 11, 23)}


def test_merge_does_not_relabel_the_rows_source(mock_db):
    """Stamping source=finnhub relabelled yfinance-shaped rows; DELL's 09-02
    row still carries the wrong label from this."""
    mock_db.find_docs.return_value = [
        {"ticker": "DELL", "snapshot_date": _dt.date(2026, 9, 2)}
    ]
    _merge_into_fundamentals("DELL", {"target_price": 556.13})

    _, _, doc = mock_db.upsert_doc.call_args[0]
    assert "source" not in doc
    assert "ticker" not in doc and "snapshot_date" not in doc


def test_merge_skips_a_ticker_with_no_snapshot(mock_db):
    mock_db.find_docs.return_value = []
    _merge_into_fundamentals("NEWCO", {"earnings_date": _dt.date(2026, 11, 23)})

    mock_db.upsert_doc.assert_not_called()


def test_merge_uses_todays_row_when_it_exists(mock_db):
    today = _dt.date.today()
    mock_db.find_docs.return_value = [{"ticker": "DELL", "snapshot_date": today}]
    _merge_into_fundamentals("DELL", {"recom_score": 1.82})

    _, key, _ = mock_db.upsert_doc.call_args[0]
    assert key["snapshot_date"] == today


def test_merge_with_nothing_to_write_does_not_touch_the_store(mock_db):
    _merge_into_fundamentals("DELL", {"target_price": None})

    mock_db.find_docs.assert_not_called()
    mock_db.upsert_doc.assert_not_called()

