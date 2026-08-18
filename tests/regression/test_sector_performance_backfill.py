import pytest
from unittest.mock import MagicMock, patch
from app.data.sector_correlation_engine import compute_all_correlations


def _mongo(sector_rows):
    """Patch the engine's Mongo layer, dispatching reads on COLLECTION name.

    This test used to patch `sector_correlation_engine.get_db` and dispatch a
    `db.execute` mock on SQL text ("SELECT sector, date, avg_return_1d" in
    query). The module calls `mongo_query`/`mongo_store` now, so the patched
    `get_db` intercepted nothing: the mock was inert, the engine read the LIVE
    database, and the regression lock below was scored against production.

    `find_rows` returns TUPLES in the requested column order — the positional
    contract of `app/db/mongo_query.py` — so the fixture returns tuples, not
    documents. The commodity leg is left empty, as it was before: this lock is
    about the SECTOR history.
    """
    query = MagicMock()

    def find_rows(collection, *a, **k):
        if collection == "sector_performance":
            return list(sector_rows)
        return []            # asset_prices: no commodity data

    query.find_rows.side_effect = find_rows
    query.join_rows.return_value = []   # price_history ⋈ ticker_metadata
    store = MagicMock()
    return patch("app.data.sector_correlation_engine.mongo_query", query), \
        patch("app.data.sector_correlation_engine.mongo_store", store), store


@pytest.mark.asyncio
async def test_regression_0_sector_correlations_computed():
    """
    REGRESSION LOCK
    Original Bug: "Computed 0 sector correlations and 1788 commodity correlations"
    Cause: The `sector_performance` table was only populated with the latest date,
    meaning it lacked the minimum 15-day history required by `compute_all_correlations`.
    As a result, `df_sector` was pivoted to a single row, and the function
    silently skipped correlation computations without throwing an error.

    Fix: Added `backfill_sector_performance` to pre-populate the historical
    returns in `sector_performance` before `compute_all_correlations` runs.

    This test verifies that `compute_all_correlations` skips correctly when data < 15 days,
    and succeeds when data is >= 15 days, mimicking the exact conditions of the bug.
    """
    # Sub-test 1: Test the bug condition (too few days in history).
    # Only 1 day of data - this was the bug state.
    too_few = [
        ("Technology", "2023-01-01", 0.05),
        ("Finance", "2023-01-01", -0.05),
    ]
    q_ctx, s_ctx, store = _mongo(too_few)
    with q_ctx, s_ctx:
        result = await compute_all_correlations()

    # Assert 0 correlations computed because it skipped due to lack of history
    assert "Computed 0 sector" in result
    # And nothing was persisted — the old test could not see this, because it
    # only counted `executemany`, which the write path no longer uses.
    assert store.upsert_doc.call_count == 0

    # Sub-test 2: Test the fixed condition (15+ days of variance history).
    # 20 days of data - this is the fixed state after backfill.
    from datetime import datetime, timedelta

    base_date = datetime(2023, 1, 1)
    enough = []
    for i in range(20):
        date_str = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
        val = 0.01 if i % 2 == 0 else -0.01
        enough.append(("Technology", date_str, val))
        enough.append(("Finance", date_str, -val))

    q_ctx, s_ctx, store = _mongo(enough)
    with q_ctx, s_ctx:
        result = await compute_all_correlations()

    # Assert > 0 correlations computed because the backfill fixed the missing history
    assert "Computed 1 sector" in result

    # And the pair was actually written. Technology and Finance were seeded as
    # exact mirrors, so the stored correlation must be -1 and classified
    # inverse — a check the old `executemany.call_count == 1` could not make.
    writes = [c[0][:3] for c in store.upsert_doc.call_args_list]
    assert len(writes) == 1
    collection, key, doc = writes[0]
    assert collection == "sector_correlations"
    assert {key["sector_a"], key["sector_b"]} == {"Technology", "Finance"}
    assert key["period"] == "30d"
    assert doc["correlation"] == pytest.approx(-1.0)
    assert doc["tier"] == "inversely_correlated"
    assert doc["data_points"] == 20
