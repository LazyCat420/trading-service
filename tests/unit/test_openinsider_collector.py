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
    """Isolate the collector's store.

    Patched `openinsider_collector.get_db` until 2026-08-18 — a symbol the module no longer
    has, so this fixture raised AttributeError at SETUP and every test using
    it ERRORED. pytest counts errors separately from failures, so the suite
    summary read "0 failed" while these never executed.

    The collector writes through mongo_store now; the yielded mock is that
    module, so assertions read the collection, key and document.
    """
    store = MagicMock()
    store.writes_mongo.return_value = True
    store.writes_pg.return_value = False
    with patch("app.collectors.openinsider_collector.mongo_store", store):
        yield store

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
                <td>M</td>
                <td>2026-05-15 18:20:10</td>
                <td>2026-05-14</td>
                <td>AAPL</td>
                <td>Apple Inc</td>
                <td>Consumer Electronics</td>
                <td>3</td>
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
    # Was `executemany` — the SQL batch write. The collector inserts into
    # `insider_trades` through mongo_store now, so assert on the collection
    # and the documents rather than on a driver method that no longer runs.
    mock_db.insert_docs.assert_called_once()
    collection, docs = mock_db.insert_docs.call_args[0][:2]
    assert collection == "insider_trades"
    assert len(docs) == count
