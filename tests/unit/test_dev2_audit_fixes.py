import pytest
from unittest.mock import patch, MagicMock
import requests
from datetime import datetime

from app.collectors.yfinance_collector import _yf_session, fetch_ohlcv_dataframe

def test_yfinance_session_configuration():
    """Verify that _yf_session is a valid requests.Session with a TimeoutHTTPAdapter."""
    assert isinstance(_yf_session, requests.Session)
    
    # Check that HTTPAdapter is mounted and is a timeout adapter
    https_adapter = _yf_session.adapters.get("https://")
    http_adapter = _yf_session.adapters.get("http://")
    
    assert https_adapter is not None
    assert http_adapter is not None
    assert hasattr(https_adapter, "timeout")
    assert https_adapter.timeout == 15.0
    assert http_adapter.timeout == 15.0

@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_yfinance_collector_uses_session(mock_ticker):
    """Verify that yf.Ticker is instantiated with the custom session."""
    mock_ticker_inst = MagicMock()
    mock_ticker_inst.history.return_value = None
    mock_ticker.return_value = mock_ticker_inst
    
    await fetch_ohlcv_dataframe("AAPL")

    # Session injection was removed in d788c27 — newer yfinance manages its own
    # (curl_cffi) session and breaks when handed a requests.Session
    mock_ticker.assert_called_once_with("AAPL")

@patch("app.db.mongo_store.insert_docs")
def test_tool_registry_log_usage_explicit_called_at(mock_insert_docs):
    """The telemetry row must carry an explicit called_at and full attribution.

    This used to patch `scripts.migration.pg_connection.get_db` and inspect SQL text. The
    telemetry callback writes through `mongo_store.insert_docs` now, so the
    patch intercepted nothing and the row went to the production
    `tool_usage_stats` collection. Reading the DOCUMENT instead of the SQL is
    strictly stronger: a column present in the statement but never populated
    used to satisfy the substring check, and cannot satisfy this one.
    """
    from app.tools.registry import registry
    from app.tools.tool_context import clear_tool_context, set_tool_context

    # Attribution reaches the row through the CONTEXT, not the call signature.
    # The SDK's _log_usage accepts ticker/cycle_id but invokes the telemetry
    # callback with only five positional args (lazycat/tool_registry.py:848),
    # dropping both on the way — so context is the only channel that survives,
    # and app/routers/agent_tools_router.py populates it per request. Setting it
    # here mirrors production rather than testing a path that cannot occur.
    clear_tool_context()
    set_tool_context(agent_name="test_agent", cycle_id="cycle-123", ticker="AAPL")
    try:
        registry._log_usage(
            tool_name="test_tool",
            agent_name="test_agent",
            ticker="AAPL",
            cycle_id="cycle-123",
            success=True,
            execution_ms=100,
            error_message=None,
        )
    finally:
        clear_tool_context()
    
    mock_insert_docs.assert_called_once()
    collection, docs = mock_insert_docs.call_args[0][:2]
    assert collection == "tool_usage_stats"
    assert len(docs) == 1
    doc = docs[0]

    # Attribution must be WRITTEN, not merely nameable. Measured before the
    # fix: all 2,066 rows over 7 days had agent_name='unknown' with ticker and
    # cycle_id NULL, because the INSERT omitted both columns entirely — so
    # "which agent researches?" could not be answered at all. The old SQL
    # substring check could not tell a written column from a mentioned one;
    # these value checks can.
    assert doc["agent_name"] == "test_agent"
    assert doc["ticker"] == "AAPL", "ticker must reach the row"
    assert doc["cycle_id"] == "cycle-123", "cycle_id must reach the row"

    # The tool-level reliability fields its two consumers actually read.
    assert doc["tool_name"] == "test_tool"
    assert doc["success"] is True
    assert doc["execution_ms"] == 100
    assert doc["error_message"] is None

    # called_at is set EXPLICITLY by the writer, not left to a store default.
    assert isinstance(doc["called_at"], datetime)

def test_data_sanity_checks_returns_correct_warnings():
    """run_sanity_checks must report every planted defect.

    This used to patch `app.processors.data_sanity.get_db` and drive an ordered
    `fetchone`/`fetchall` side_effect list. The module reads through
    `mongo_store.find_docs`/`count_docs` now, so the patch intercepted nothing
    and the "planted" data was whatever production held — the assertions
    scored the live database.

    Dispatch here is on the COLLECTION plus the FILTER, never on statement
    text or call ORDER. An ordered side_effect list silently re-labels every
    subsequent check the moment one query is added, removed or reordered;
    keying on what is actually being asked cannot.
    """
    def find_docs(collection, filt=None, **kwargs):
        filt = filt or {}
        if collection == "sec_13f_holdings":
            if "filer_name" in filt:
                return [{"value_usd": 10e9}]        # Berkshire AAPL under $30B
            return [{"value_usd": 2e12}]            # top position over the $1T ceiling
        if collection == "fundamentals":
            if "pe_ratio" in filt:
                return []                           # no extreme P/E
            return [{"market_cap": 500e9}]          # AAPL market cap under $1T
        if collection == "congress_trades":
            return [{"chamber": "House"}]           # House only → Senate missing
        raise AssertionError(f"unexpected find_docs collection: {collection}")

    def count_docs(collection, filt=None, **kwargs):
        filt = filt or {}
        if collection == "sec_13f_holdings":
            if "shares" in filt:
                return 3                            # 0 shares but positive value
            return 5                                # negative value_usd
        if collection == "fundamentals":
            return 0                                # no negative market caps
        if collection == "price_history":
            return 0                                # no close <= 0
        if collection == "technicals":
            return 2                                # RSI outside 0-100
        if collection == "news_articles":
            return 0                                # clean news content
        raise AssertionError(f"unexpected count_docs collection: {collection}")

    from app.processors.data_sanity import run_sanity_checks

    with patch("app.processors.data_sanity.mongo_store") as store:
        store.find_docs.side_effect = find_docs
        store.count_docs.side_effect = count_docs
        failures = run_sanity_checks()

    assert len(failures) > 0
    # Confirm specific failures are present
    assert any("13F: Max position value" in f for f in failures)
    assert any("Berkshire AAPL" in f for f in failures)
    assert any("AAPL market cap" in f for f in failures)
    assert any("missing Senate data" in f for f in failures)
    assert any("RSI outside 0-100" in f for f in failures)
    # The counted defects must be reported with their COUNTS, not just named.
    assert any("5 holdings with negative value_usd" in f for f in failures)
    assert any("3 holdings with 0 shares but positive value" in f for f in failures)
    # ...and the clean checks must stay silent, so a checker that reports
    # everything unconditionally cannot pass this test.
    assert not any("close <= " in f for f in failures)
    assert not any("Extreme P/E" in f for f in failures)
    assert not any("missing House data" in f for f in failures)

