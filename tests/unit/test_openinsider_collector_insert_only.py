"""The collector must not be able to re-create the duplicates.

`insert_docs` is documented as PG's `ON CONFLICT (id) DO NOTHING` because it
swallows duplicate-key errors. That is only DO NOTHING if the server RAISES
one, i.e. only behind a unique index. `insider_trades.natural_key` was created
without `unique`, so the swallow never fired and every pass appended a copy:
2,446 documents for 356 ids, 45 ids stored 23 times each.

`bulk_upsert(..., insert_only=True)` is the codebase's existing idiom for the
same semantic and does NOT depend on an index to be correct — it is the shape
`polygon_collector.py:94` and `news_collector.py:767` already use.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collectors import openinsider_collector

HTML = """
<table class="tinytable">
 <thead><tr><th>Header</th></tr></thead>
 <tbody>
  <tr><td>M</td><td>2026-05-15 18:20:10</td><td>2026-05-14</td><td>AAPL</td>
      <td>Apple Inc</td><td>Consumer Electronics</td><td>3</td>
      <td>P - Purchase</td><td>$180.00</td><td>+10,000</td><td>100,000</td>
      <td>+10%</td><td>+$1,800,000</td></tr>
 </tbody>
</table>
"""


@pytest.fixture
def store():
    s = MagicMock()
    s.writes_mongo.return_value = True
    s.writes_pg.return_value = False
    with patch("app.collectors.openinsider_collector.mongo_store", s):
        yield s


@pytest.mark.asyncio
@patch("app.collectors.openinsider_collector._fetch_html")
async def test_writes_through_bulk_upsert_insert_only(mock_fetch, store):
    mock_fetch.return_value = HTML
    count = await collect(mock_fetch)
    assert count == 1
    store.insert_docs.assert_not_called()
    store.bulk_upsert.assert_called_once()
    args, kwargs = store.bulk_upsert.call_args
    assert args[0] == "insider_trades"
    assert kwargs.get("insert_only") is True, (
        "without insert_only the write is $set, i.e. DO UPDATE — the opposite "
        "of the comment, and it would overwrite the collected_at the archive "
        "validates first-by-_id on"
    )
    assert kwargs.get("key_field") == "id"


async def collect(_mock_fetch):
    return await openinsider_collector.collect_cluster_buys(days=30)


@pytest.mark.asyncio
@patch("app.collectors.openinsider_collector._fetch_html")
async def test_two_passes_submit_the_same_key_not_two_rows(mock_fetch, store):
    """The regression itself: the same scrape twice must key on the same `id`
    both times, so the second pass is an upsert onto the first."""
    mock_fetch.return_value = HTML
    await openinsider_collector.collect_cluster_buys(days=30)
    await openinsider_collector.collect_cluster_buys(days=30)
    assert store.bulk_upsert.call_count == 2
    ids = [c[0][1][0]["id"] for c in store.bulk_upsert.call_args_list]
    assert ids[0] == ids[1]


def test_the_do_nothing_comment_no_longer_claims_insert_docs():
    """defect: the comment was a character-for-character clone of the
    asset_prices one, and it described a guarantee the index did not provide."""
    src = Path(inspect.getfile(openinsider_collector)).read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    names = {n.func.attr for n in calls
             if isinstance(n.func.value, ast.Name) and n.func.value.id == "mongo_store"}
    assert "insert_docs" not in names
    assert "bulk_upsert" in names
