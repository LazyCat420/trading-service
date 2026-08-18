"""The watchlist price refresh must cover what the S&P 500 loop does not.

2026-07-26: `price_history` had two populations. BootService's
_sp500_daily_refresh_loop kept the ~509 S&P 500 tickers at p90 = 0 trading days
old, while the other ~2,237 tickers had no scheduled writer at all and sat
frozen at 2026-07-17. 17 of the 45 ACTIVE watchlist tickers had drifted 4-63
days stale, and ASIC reached a 68-confidence BUY with zero price rows on the
desk.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.cycle_scheduler import SchedulerService


def _dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _mongo(watchlist=(), etfs=(), last_bars=None):
    """Patch mongo_store AND mongo_query on cycle_scheduler, dispatching on
    COLLECTION name — patching only the reads would leave writes pointed at the
    real store.

    `watchlist` is [(ticker, status)]; `last_bars` maps ticker → newest bar date
    (absent = no price history at all, the ASIC case).
    """
    store = MagicMock()
    query = MagicMock()
    last_bars = last_bars or {}

    def distinct_values(collection, field, q=None):
        q = q or {}
        if collection == "watchlist":
            want = q.get("status")
            allowed = want.get("$in") if isinstance(want, dict) else [want]
            return [t for t, s in watchlist if s in allowed]
        if collection == "ticker_metadata":
            return list(etfs)
        return []

    def group_rows(collection, q, keys, aggs, select, sort=None, limit=0):
        if collection == "price_history":
            return [(t, v) for t, v in last_bars.items()]
        return []

    store.distinct_values.side_effect = distinct_values
    query.group_rows.side_effect = group_rows
    return store, query


def _patched(store, query):
    return (patch("app.services.cycle_scheduler.mongo_store", store),
            patch("app.services.cycle_scheduler.mongo_query", query))


@pytest.mark.asyncio
async def test_refresh_fetches_each_stale_ticker():
    store, query = _mongo(
        watchlist=[("ASIC", "active"), ("RH", "active")],
        last_bars={"RH": _dt("2026-07-20")},
    )
    ps, pq = _patched(store, query)
    with ps, pq, patch("app.collectors.data_rotator.fetch_price_history",
                       new_callable=AsyncMock) as fetch:
        fetch.return_value = 124
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    # ASIC has no price history at all → NULLS FIRST puts it ahead of RH.
    assert [c.args[0] for c in fetch.call_args_list] == ["ASIC", "RH"]


@pytest.mark.asyncio
async def test_query_orders_stalest_first_and_covers_paused():
    """Ordering is the whole design: stale-first converges the list in a couple
    of nights instead of round-robining forever. NULLS FIRST puts tickers with
    NO price history at the very front — the ASIC case. Paused watchlist
    entries are in scope; every other status is not."""
    store, query = _mongo(
        watchlist=[("FRESH", "active"), ("PAUSEDOLD", "paused"),
                   ("MID", "active"), ("NOBARS", "active"),
                   ("GONE", "removed")],
        last_bars={"FRESH": _dt("2026-07-25"),
                   "MID": _dt("2026-07-21"),
                   "PAUSEDOLD": _dt("2026-07-02")},
    )
    ps, pq = _patched(store, query)
    with ps, pq, patch("app.collectors.data_rotator.fetch_price_history",
                       new_callable=AsyncMock) as fetch:
        fetch.return_value = 1
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    order = [c.args[0] for c in fetch.call_args_list]
    # NULLS FIRST, then oldest bar first.
    assert order == ["NOBARS", "PAUSEDOLD", "MID", "FRESH"]
    # Scoped to the watchlist: a non-active/paused status is never swept.
    assert "GONE" not in order


@pytest.mark.asyncio
async def test_etfs_are_covered():
    """ETFs have no other scheduled price writer, so the universe unions them
    in — without it the screener's ETF rows go stale and its fresh-prices gate
    hides them all."""
    store, query = _mongo(watchlist=[], etfs=["SPY"])
    ps, pq = _patched(store, query)
    with ps, pq, patch("app.collectors.data_rotator.fetch_price_history",
                       new_callable=AsyncMock) as fetch:
        fetch.return_value = 1
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    assert [c.args[0] for c in fetch.call_args_list] == ["SPY"]


@pytest.mark.asyncio
async def test_batch_limit_is_respected():
    store, query = _mongo(
        watchlist=[(f"T{i}", "active") for i in range(10)])
    ps, pq = _patched(store, query)
    with ps, pq, patch("app.collectors.data_rotator.fetch_price_history",
                       new_callable=AsyncMock) as fetch:
        fetch.return_value = 1
        await SchedulerService._run_watchlist_price_refresh(batch=3)

    assert len(fetch.call_args_list) == 3


@pytest.mark.asyncio
async def test_zero_rows_counts_as_no_data_not_success():
    """fetch_price_history swallows provider errors and returns 0, so a
    falsy return is the failure signal — treating it as success is exactly how
    a total outage reported collector_error=0 on 2026-07-26."""
    store, query = _mongo(
        watchlist=[("GOOD", "active"), ("DEAD", "active")])

    async def _fetch(ticker, *a, **k):
        return 124 if ticker == "GOOD" else 0

    ps, pq = _patched(store, query)
    with ps, pq, \
         patch("app.collectors.data_rotator.fetch_price_history", new=_fetch), \
         patch("app.services.cycle_scheduler.logger") as log:
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    msg = log.info.call_args.args[0] % log.info.call_args.args[1:]
    assert "1/2" in msg
    assert "DEAD" in msg
    assert "GOOD" not in msg


@pytest.mark.asyncio
async def test_one_bad_ticker_does_not_abort_the_run():
    store, query = _mongo(
        watchlist=[("BOOM", "active"), ("FINE", "active")])

    async def _fetch(ticker, *a, **k):
        if ticker == "BOOM":
            raise RuntimeError("provider exploded")
        return 124

    ps, pq = _patched(store, query)
    with ps, pq, \
         patch("app.collectors.data_rotator.fetch_price_history", new=_fetch), \
         patch("app.services.cycle_scheduler.logger") as log:
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    msg = log.info.call_args.args[0] % log.info.call_args.args[1:]
    assert "1/2" in msg


@pytest.mark.asyncio
async def test_db_failure_is_swallowed():
    """A scheduler job must never propagate — an exception here would kill the
    job and silently stop every future refresh."""
    store = MagicMock()
    store.distinct_values.side_effect = RuntimeError("no db")
    with patch("app.services.cycle_scheduler.mongo_store", store), \
         patch("app.services.cycle_scheduler.logger") as log:
        await SchedulerService._run_watchlist_price_refresh(batch=40)

    log.error.assert_called_once()
