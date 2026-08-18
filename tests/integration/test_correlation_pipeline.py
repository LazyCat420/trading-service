import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from app.data import sector_aggregator, sector_correlation_engine
from app.data.sector_aggregator import backfill_sector_performance
from app.data.sector_correlation_engine import compute_all_correlations


@pytest.mark.asyncio
async def test_correlation_computes_with_backfilled_data():
    """
    Simulate startup:
    1. backfill_sector_performance runs on 20 days of data
    2. compute_all_correlations runs
    Assert that the correlations actually compute instead of skipping.

    Both modules read through `mongo_query` and write through
    `mongo_store.upsert_doc` now. The reads used to be dispatched on SQL
    substrings and the writes read off an `executemany` parameter list; both
    are keyed on the COLLECTION NAME here, and the written row is checked by
    field name rather than by tuple position.
    """
    base_date = datetime(2023, 1, 1)
    mock_dates = [(base_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(20)]

    # price_history JOIN ticker_metadata, as `join_rows` returns it:
    # tuples in the requested select order (ticker, date, close, sector).
    backfill_rows = []
    for i, date_str in enumerate(mock_dates):
        backfill_rows.append(("AAPL", date_str, 100.0 + i, "Technology"))
    for i, date_str in enumerate(mock_dates):
        backfill_rows.append(("JPM", date_str, 200.0 - i, "Finance"))

    # ── 1. Backfill ──────────────────────────────────────────────────────
    agg_query = MagicMock()
    # No existing sector_performance history, so the backfill must not skip.
    agg_query.agg_row.return_value = (0,)
    agg_query.join_rows.return_value = backfill_rows

    agg_store = MagicMock()
    backfilled = []
    agg_store.upsert_doc.side_effect = (
        lambda collection, key, doc, **_kw: backfilled.append((collection, key, doc))
    )

    with patch.object(sector_aggregator, "mongo_query", agg_query), \
         patch.object(sector_aggregator, "mongo_store", agg_store):
        await backfill_sector_performance()

    # 19 days (20 - 1, pct_change drops the first) * 2 sectors = 38 rows.
    assert len(backfilled) == 38
    assert {c for c, _k, _d in backfilled} == {"sector_performance"}
    assert {d["sector"] for _c, _k, d in backfilled} == {"Technology", "Finance"}
    # Each row is keyed on the sector-day it describes, so a re-run updates
    # rather than duplicating.
    assert all(set(k) == {"sector", "date"} for _c, k, _d in backfilled)

    # ── 2. Correlations ──────────────────────────────────────────────────
    # What the backfill just wrote, read back the way the engine reads it:
    # find_rows("sector_performance", ...) -> (sector, date, avg_return_1d).
    # Alternating sign gives the pair a real, non-NaN correlation.
    sector_rows = []
    for i, date_str in enumerate(mock_dates[1:]):
        val = 0.01 if i % 2 == 0 else -0.01
        sector_rows.append(("Technology", date_str, val))
        sector_rows.append(("Finance", date_str, -val))

    def _find_rows(collection, *_a, **_kw):
        if collection == "sector_performance":
            return sector_rows
        return []          # asset_prices: no commodities in this scenario

    corr_query = MagicMock()
    corr_query.find_rows.side_effect = _find_rows
    corr_query.join_rows.return_value = []   # no stock rows -> no commodity pairs

    corr_store = MagicMock()
    written = []
    corr_store.upsert_doc.side_effect = (
        lambda collection, key, doc, **_kw: written.append((collection, key, doc))
    )

    with patch.object(sector_correlation_engine, "mongo_query", corr_query), \
         patch.object(sector_correlation_engine, "mongo_store", corr_store):
        result = await compute_all_correlations()

    # 19 days is > 15 (half of the 30d window) but NOT > 45 (half of 90d), so
    # only the 30d correlation for the single (Technology, Finance) pair is
    # written.
    assert len(written) == 1
    collection, key, doc = written[0]
    assert collection == "sector_correlations"
    assert {doc["sector_a"], doc["sector_b"]} == {"Technology", "Finance"}
    assert doc["period"] == "30d"
    assert key == {
        "sector_a": doc["sector_a"], "sector_b": doc["sector_b"], "period": "30d"
    }
    assert doc["data_points"] == 19
    # The pair was constructed as exact mirrors, so it must come back as a
    # strong inverse — a correlation computed over the wrong axis would not.
    assert doc["correlation"] == pytest.approx(-1.0)
    assert doc["tier"] == "inversely_correlated"

    assert "Computed 1 sector & 0 comm correlations" in result
