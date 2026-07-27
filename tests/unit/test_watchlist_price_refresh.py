"""The watchlist price refresh must cover what the S&P 500 loop does not.

2026-07-26: `price_history` had two populations. BootService's
_sp500_daily_refresh_loop kept the ~509 S&P 500 tickers at p90 = 0 trading days
old, while the other ~2,237 tickers had no scheduled writer at all and sat
frozen at 2026-07-17. 17 of the 45 ACTIVE watchlist tickers had drifted 4-63
days stale, and ASIC reached a 68-confidence BUY with zero price rows on the
desk.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.cycle_scheduler import SchedulerService


def _db_returning(rows):
    db = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    db.execute.return_value = cursor
    ctx = MagicMock()
    ctx.__enter__.return_value = db
    return ctx, db


@pytest.mark.asyncio
async def test_refresh_fetches_each_stale_ticker():
    ctx, _db = _db_returning([("ASIC", None), ("RH", "2026-07-20")])

    with patch("app.db.connection.get_db", return_value=ctx), \
         patch("app.collectors.data_rotator.fetch_price_history",
               new_callable=AsyncMock) as fetch:
        fetch.return_value = 124
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    assert [c.args[0] for c in fetch.call_args_list] == ["ASIC", "RH"]


@pytest.mark.asyncio
async def test_query_orders_stalest_first_and_covers_paused():
    """Ordering is the whole design: stale-first converges the list in a couple
    of nights instead of round-robining forever. NULLS FIRST puts tickers with
    NO price history at the very front — the ASIC case."""
    ctx, db = _db_returning([])

    with patch("app.db.connection.get_db", return_value=ctx), \
         patch("app.collectors.data_rotator.fetch_price_history",
               new_callable=AsyncMock):
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    sql = " ".join(db.execute.call_args.args[0].split())
    assert "ORDER BY last_bar ASC NULLS FIRST" in sql
    assert "IN ('active', 'paused')" in sql
    # Scoped to the watchlist: sweeping all ~2,700 tickers nightly would spend
    # the provider budget on a population no cycle reads.
    assert "FROM watchlist" in sql


@pytest.mark.asyncio
async def test_zero_rows_counts_as_no_data_not_success():
    """fetch_price_history swallows provider errors and returns 0, so a
    falsy return is the failure signal — treating it as success is exactly how
    a total outage reported collector_error=0 on 2026-07-26."""
    ctx, _db = _db_returning([("GOOD", None), ("DEAD", None)])

    async def _fetch(ticker, *a, **k):
        return 124 if ticker == "GOOD" else 0

    with patch("app.db.connection.get_db", return_value=ctx), \
         patch("app.collectors.data_rotator.fetch_price_history", new=_fetch), \
         patch("app.services.cycle_scheduler.logger") as log:
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    msg = log.info.call_args.args[0] % log.info.call_args.args[1:]
    assert "1/2" in msg
    assert "DEAD" in msg
    assert "GOOD" not in msg


@pytest.mark.asyncio
async def test_one_bad_ticker_does_not_abort_the_run():
    ctx, _db = _db_returning([("BOOM", None), ("FINE", None)])

    async def _fetch(ticker, *a, **k):
        if ticker == "BOOM":
            raise RuntimeError("provider exploded")
        return 124

    with patch("app.db.connection.get_db", return_value=ctx), \
         patch("app.collectors.data_rotator.fetch_price_history", new=_fetch), \
         patch("app.services.cycle_scheduler.logger") as log:
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    msg = log.info.call_args.args[0] % log.info.call_args.args[1:]
    assert "1/2" in msg


@pytest.mark.asyncio
async def test_db_failure_is_swallowed():
    """A scheduler job must never propagate — an exception here would kill the
    job and silently stop every future refresh."""
    with patch("app.db.connection.get_db", side_effect=RuntimeError("no db")), \
         patch("app.services.cycle_scheduler.logger") as log:
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    log.error.assert_called_once()
