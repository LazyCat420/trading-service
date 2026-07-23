import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import datetime

from app.collectors.openinsider_collector import (
    collect_cluster_buys,
    collect_all,
    clean_float,
    clean_int,
)

@pytest.fixture
def mock_db():
    with patch("app.collectors.openinsider_collector.get_db") as mock_get_db:
        db = MagicMock()
        db.fetchone.return_value = None
        db.fetchall.return_value = []
        mock_get_db.return_value.__enter__.return_value = db
        yield db

def test_clean_helpers():
    assert clean_float("$1,234.56") == 1234.56
    assert clean_float("+$0.50") == 0.50
    assert clean_int("+1,234") == 1234
    assert clean_int("-50") == -50

@pytest.mark.asyncio
@patch("app.collectors.openinsider_collector._fetch_html")
async def test_collect_cluster_buys_success(mock_fetch, mock_db):
    html_content = """
    <table class="tinytable">
        <thead><tr><th>Header</th></tr></thead>
        <tbody>
            <tr>
                <td>details</td>
                <td>2026-05-15 18:20:10</td>
                <td>2026-05-14</td>
                <td>AAPL</td>
                <td>Cook Timothy D</td>
                <td>CEO</td>
                <td>P - Purchase</td>
                <td>$180.00</td>
                <td>+10,000</td>
                <td>100,000</td>
                <td>+10%</td>
                <td>+$1,800,000</td>
            </tr>
        </tbody>
    </table>
    """
    mock_fetch.return_value = html_content

    count = await collect_cluster_buys(days=30)
    assert count == 1
    mock_db.executemany.assert_called_once()
