import pytest
from unittest.mock import patch, MagicMock
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
@patch("app.services.scraper_client.scraper_client.scrape")
async def test_collect_economic_calendar_success(mock_scrape, mock_db):
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
    mock_scrape.return_value = {"success": True, "content": html_content}

    count = await collect_economic_calendar()
    assert count == 1
    mock_db.executemany.assert_called_once()
