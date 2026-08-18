"""FMP collector tests, ported off the inert `get_db` mock.

These used to patch `app.collectors.fmp_collector.get_db` and assert on
`db.execute(...)`. The Postgres->Mongo migration removed that symbol from the
module — it writes through `mongo_store.upsert_doc` now — so the patch target
no longer existed and the writes went to the LIVE Mongo database while the
tests still looked isolated. They patch `mongo_store` here instead, and the
old "one execute call" assertions became structural assertions on the
collection name, the upsert KEY and the document FIELDS, which pin far more
than a call count did.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import datetime

from app.collectors.fmp_collector import (
    collect_congress_trades,
    collect_price_history,
    collect_fundamentals,
    collect_financials,
    collect_balance_sheet,
    collect_all,
)


@pytest.fixture
def ms():
    """Patch the Mongo write surface the collector actually uses.

    `fmp_collector` imports only `mongo_store` (no reads), so this is the whole
    database surface of the module.
    """
    with patch("app.collectors.fmp_collector.mongo_store") as store:
        yield store


def _upserts(store, collection):
    """Every upsert_doc call aimed at `collection`, as (key, doc) pairs."""
    return [
        (c[0][1], c[0][2])
        for c in store.upsert_doc.call_args_list
        if c[0][0] == collection
    ]


@pytest.mark.asyncio
@patch("app.collectors.fmp_collector._get_key", return_value="fake_key")
@patch("app.collectors.fmp_collector.httpx.AsyncClient")
async def test_collect_congress_trades_success(mock_client_class, mock_get_key, ms):

    mock_client = MagicMock()
    mock_client.get = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"ticker": "AAPL", "senator": "John Doe", "transaction_date": "2023-10-01", "type": "Purchase", "amount": "$1,000 - $15,000"},
        {"ticker": "MSFT", "representative": "Jane Smith", "transaction_date": "2023-10-02", "type": "Sale", "amount": "$15,001 - $50,000"}
    ]
    mock_client.get.return_value = mock_resp

    # Test with filter: only the AAPL row may be written, MSFT is filtered out.
    count = await collect_congress_trades("AAPL")

    assert count == 1
    writes = _upserts(ms, "congress_trades")
    assert len(writes) == 1
    _key, doc = writes[0]
    # The old test asserted "AAPL" appeared somewhere in the SQL parameters;
    # naming the field is the same check, only stronger.
    assert doc["ticker"] == "AAPL"
    assert doc["politician"] == "John Doe"
    assert doc["transaction_type"] == "Purchase"
    assert doc["amount_range"] == "$1,000 - $15,000"


@pytest.mark.asyncio
@patch("app.collectors.fmp_collector._get_key", return_value="fake_key")
@patch("app.collectors.fmp_collector.httpx.AsyncClient")
async def test_collect_congress_trades_403(mock_client_class, mock_get_key, ms):
    mock_client = MagicMock()
    mock_client.get = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_client.get.return_value = mock_resp

    count = await collect_congress_trades("AAPL")
    assert count == 0
    # A refused upstream must not write anything.
    ms.upsert_doc.assert_not_called()


@pytest.mark.asyncio
@patch("app.collectors.fmp_collector._get_key", return_value="fake_key")
@patch("app.services.request_utils.SmartClient")
async def test_collect_price_history_success(mock_smart_client, mock_get_key, ms):

    mock_client = MagicMock()
    mock_client.get = AsyncMock()
    mock_smart_client.return_value.__aenter__.return_value = mock_client

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    # Provide a date that is clearly within the default 365 days cutoff
    today = datetime.date.today()
    mock_resp.json.return_value = {
        "historical": [
            {"date": today.isoformat(), "open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000}
        ]
    }
    mock_client.get.return_value = mock_resp

    count = await collect_price_history("AAPL")

    assert count == 1
    writes = _upserts(ms, "price_history")
    assert len(writes) == 1
    key, doc = writes[0]
    # The upsert key is what makes a re-run idempotent per (ticker, date, source).
    assert key == {"ticker": "AAPL", "date": today, "source": "fmp"}
    assert doc["open"] == 100.0
    assert doc["high"] == 105.0
    assert doc["low"] == 95.0
    assert doc["close"] == 102.0
    assert doc["volume"] == 1000
    assert doc["source"] == "fmp"


@pytest.mark.asyncio
@patch("app.collectors.fmp_collector._get_key", return_value="fake_key")
@patch("app.services.request_utils.SmartClient")
async def test_collect_fundamentals_success(mock_smart_client, mock_get_key, ms):

    mock_client = MagicMock()
    mock_client.get = AsyncMock()
    mock_smart_client.return_value.__aenter__.return_value = mock_client

    mock_prof_resp = MagicMock()
    mock_prof_resp.status_code = 200
    mock_prof_resp.json.return_value = [{"mktCap": 1000000, "beta": 1.2}]

    mock_metrics_resp = MagicMock()
    mock_metrics_resp.status_code = 200
    mock_metrics_resp.json.return_value = [{"peRatioTTM": 15.0}]

    mock_client.get.side_effect = [mock_prof_resp, mock_metrics_resp]

    result = await collect_fundamentals("AAPL")

    assert result is True
    writes = _upserts(ms, "fundamentals")
    assert len(writes) == 1
    key, doc = writes[0]
    assert key["ticker"] == "AAPL"
    assert doc["source"] == "fmp"
    # Both responses have to reach the document — profile AND key-metrics.
    assert doc["market_cap"] == 1000000
    assert doc["beta"] == 1.2
    assert doc["pe_ratio"] == 15.0


@pytest.mark.asyncio
@patch("app.collectors.fmp_collector._get_key", return_value="fake_key")
@patch("app.services.request_utils.SmartClient")
async def test_collect_financials_success(mock_smart_client, mock_get_key, ms):

    mock_client = MagicMock()
    mock_client.get = AsyncMock()
    mock_smart_client.return_value.__aenter__.return_value = mock_client

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"date": "2023-10-01", "revenue": 1000, "grossProfit": 500, "operatingIncome": 200, "netIncome": 100, "eps": 1.5}
    ]
    mock_client.get.return_value = mock_resp

    count = await collect_financials("AAPL")

    assert count == 1
    writes = _upserts(ms, "financial_history")
    assert len(writes) == 1
    key, doc = writes[0]
    assert key["ticker"] == "AAPL"
    assert key["period_type"] == "quarterly"
    assert doc["revenue"] == 1000
    assert doc["gross_profit"] == 500
    assert doc["operating_income"] == 200
    assert doc["net_income"] == 100
    assert doc["eps"] == 1.5


@pytest.mark.asyncio
@patch("app.collectors.fmp_collector._get_key", return_value="fake_key")
@patch("app.services.request_utils.SmartClient")
async def test_collect_balance_sheet_success(mock_smart_client, mock_get_key, ms):

    mock_client = MagicMock()
    mock_client.get = AsyncMock()
    mock_smart_client.return_value.__aenter__.return_value = mock_client

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"date": "2023-10-01", "totalAssets": 1000, "totalLiabilities": 500, "totalStockholdersEquity": 500, "cashAndCashEquivalents": 100, "totalDebt": 200}
    ]
    mock_client.get.return_value = mock_resp

    count = await collect_balance_sheet("AAPL")

    assert count == 1
    writes = _upserts(ms, "balance_sheet")
    assert len(writes) == 1
    key, doc = writes[0]
    assert key["ticker"] == "AAPL"
    assert doc["total_assets"] == 1000
    assert doc["total_liabilities"] == 500
    assert doc["total_equity"] == 500
    assert doc["cash"] == 100
    assert doc["total_debt"] == 200
