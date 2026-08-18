"""backfill_sector_performance: skip, no-data, and the arithmetic.

These used to patch `sector_aggregator.get_db` and dispatch a `db.execute`
mock on SQL text ("COUNT(DISTINCT date)" in query), then assert on the
`executemany` payload. `app/data/sector_aggregator.py` calls
`mongo_query`/`mongo_store` now, so the patched `get_db` intercepted nothing:
the mock was inert and the assertions were scored against the live database.

Rewritten against the Mongo layer, dispatching reads on the COLLECTION name.
The write assertions are stronger for it — the backfill upserts one document
per sector-day, so the test now reads the upsert KEY and the stored fields
rather than a positional tuple whose meaning depended on the column order in
a SQL string.
"""
import pytest
from unittest.mock import MagicMock, patch

from app.data.sector_aggregator import backfill_sector_performance


def _mongo(distinct_dates=0, join_rows=None):
    """Patch the module's Mongo read and write helpers together.

    `agg_row('sector_performance', {}, [('count_distinct', 'date')])` is the
    history probe that decides whether to skip; `join_rows` is the
    price_history ⋈ ticker_metadata read that feeds the arithmetic.
    """
    query = MagicMock()
    query.agg_row.return_value = (distinct_dates,)
    query.join_rows.return_value = list(join_rows or [])
    store = MagicMock()
    return patch("app.data.sector_aggregator.mongo_query", query), \
        patch("app.data.sector_aggregator.mongo_store", store), query, store


def _upserts(store):
    """(key, doc) for every sector_performance upsert."""
    out = []
    for c in store.upsert_doc.call_args_list:
        collection, key, doc = c[0][:3]
        assert collection == "sector_performance"
        out.append((key, doc))
    return out


@pytest.mark.asyncio
async def test_backfill_sector_performance_empty_db():
    """Ensure backfill handles an empty price_history gracefully."""
    q_ctx, s_ctx, query, store = _mongo(distinct_dates=0, join_rows=[])
    with q_ctx, s_ctx:
        await backfill_sector_performance()

    # It must have got past the skip check and actually looked at prices...
    assert query.join_rows.call_count == 1
    # ...and written nothing, because there was nothing to compute.
    assert store.upsert_doc.call_count == 0


@pytest.mark.asyncio
async def test_backfill_sector_performance_populates_history():
    """Ensure backfill calculates and inserts historical data."""
    # 2 days of data for 2 tickers in the same sector. The join returns tuples
    # in the select order (ticker, date, close, sector).
    rows = [
        ("AAPL", "2023-01-01", 100.0, "Technology"),
        ("AAPL", "2023-01-02", 105.0, "Technology"),
        ("MSFT", "2023-01-01", 200.0, "Technology"),
        ("MSFT", "2023-01-02", 210.0, "Technology"),
    ]
    q_ctx, s_ctx, query, store = _mongo(distinct_dates=0, join_rows=rows)
    with q_ctx, s_ctx:
        await backfill_sector_performance()

    written = _upserts(store)

    # We expect 1 row written since there's only 1 day of returns (the second
    # day). Day 1 has no return since pct_change requires a previous row.
    assert len(written) == 1
    key, doc = written[0]
    # The upsert must be keyed on the sector-day identity, or a re-run would
    # append duplicate history instead of replacing it.
    assert key == {"sector": "Technology", "date": "2023-01-02"}
    assert doc["sector"] == "Technology"
    assert doc["date"] == "2023-01-02"
    # AAPL went 100 -> 105 (5%), MSFT went 200 -> 210 (5%), average is 5% (0.05)
    assert doc["avg_return_1d"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_backfill_skips_if_history_exists():
    """Ensure backfill skips if history is already present."""
    q_ctx, s_ctx, query, store = _mongo(distinct_dates=2, join_rows=[])
    with q_ctx, s_ctx:
        await backfill_sector_performance()

    # price history should not be queried and nothing should be written
    assert query.join_rows.call_count == 0
    assert store.upsert_doc.call_count == 0
