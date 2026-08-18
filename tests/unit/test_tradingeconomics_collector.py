import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import datetime

from app.collectors.tradingeconomics_collector import (
    collect_economic_calendar,
    collect_all,
    parse_val,
)

@pytest.fixture
def mock_db():
    with patch("app.collectors.tradingeconomics_collector.get_db") as mock_get_db:
        db = MagicMock()
        db.fetchone.return_value = None
        db.fetchall.return_value = []
        mock_get_db.return_value.__enter__.return_value = db
        yield db

def test_parse_val():
    assert parse_val("1.2B") == 1200000000.0
    assert parse_val("150M") == 150000000.0
    assert parse_val("-0.5%") == -0.5
    assert parse_val("10K") == 10000.0
    assert parse_val("-") is None

@pytest.mark.asyncio
@patch("app.collectors.tradingeconomics_collector._collect_forexfactory")
@patch("httpx.AsyncClient")
async def test_collect_economic_calendar_te_fallback(mock_client_cls, mock_ff, mock_db):
    html_content = """
    <table id="calendar">
        <tr class="table-header"><td>Monday May 15 2026</td></tr>
        <tr class="calendar-row">
            <td>08:30 AM</td>
            <td>US</td>
            <td>Retail Sales MoM <span class="calendar-importance high"></span></td>
            <td>0.4%</td>
            <td>0.2%</td>
            <td>0.1%</td>
        </tr>
    </table>
    """
    mock_ff.return_value = 0  # ForexFactory primary empty → TE HTML fallback
    resp = MagicMock()
    resp.text = html_content
    resp.raise_for_status.return_value = None
    client = mock_client_cls.return_value.__aenter__.return_value
    client.get = AsyncMock(return_value=resp)

    count = await collect_economic_calendar()
    assert count == 1
    mock_db.executemany.assert_called_once()


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_collect_forexfactory_primary(mock_client_cls, mock_db):
    from app.collectors.tradingeconomics_collector import _collect_forexfactory

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = [
        {"title": "CPI y/y", "country": "USD", "date": "2026-07-24T08:30:00-04:00",
         "impact": "High", "forecast": "2.9%", "previous": "3.0%"},
        {"title": "", "country": "USD", "date": "2026-07-24T08:30:00-04:00"},  # skipped
    ]
    client = mock_client_cls.return_value.__aenter__.return_value
    client.get = AsyncMock(return_value=resp)

    count = await _collect_forexfactory()
    assert count == 1
    mock_db.executemany.assert_called_once()
