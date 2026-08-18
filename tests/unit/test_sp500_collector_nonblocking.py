"""The bulk S&P 500 refresh must not block the event loop.

2026-07-27: every deploy left trading-service UNHEALTHY for ~2 minutes.
`collect_sp500_prices` is async and runs from a background task, but it called
`yf.download` (synchronous network I/O, ~7s per 100-ticker chunk) and then
~2,000 synchronous DB inserts directly on the event loop. Six chunks in a row
meant the loop never got a turn, so the HTTP server sharing that loop stopped
answering /health and Docker's healthcheck failed three times running.

CPU sat at 23% throughout — it was never CPU-bound. The loop was simply never
scheduled. That is why the fix is asyncio.to_thread and not a longer
healthcheck timeout: the healthcheck was telling the truth.

These tests assert the PROPERTY (the loop stays responsive), not the
implementation, so they still pass if the offload strategy changes.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def _fake_frame(tickers):
    """A minimal multi-ticker yfinance frame."""
    idx = pd.to_datetime(["2026-07-23", "2026-07-24"])
    cols = pd.MultiIndex.from_product(
        [tickers, ["Open", "High", "Low", "Close", "Volume"]]
    )
    data = [[1.0, 2.0, 0.5, 1.5, 100] * len(tickers)] * 2
    return pd.DataFrame(data, index=idx, columns=cols)


@pytest.fixture
def db_ctx():
    db = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = [("AAA",), ("BBB",)]
    db.execute.return_value = cursor
    ctx = MagicMock()
    ctx.__enter__.return_value = db
    return ctx, db


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_download(db_ctx):
    """A blocking download must not stall a concurrent loop task.

    The heartbeat ticks every 10ms while a 300ms 'download' runs. On the
    event loop it would tick ~0 times; off it, many. The threshold is
    deliberately loose (>=5) so this measures blocking, not scheduler jitter.
    """
    ctx, _db = db_ctx
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_download(*a, **k):
        time.sleep(0.3)          # synchronous, like the real yf.download
        return _fake_frame(["AAA", "BBB"])

    with patch("app.data.sp500_price_collector.get_db", return_value=ctx), \
         patch("app.data.sp500_price_collector.yf.download", side_effect=slow_download), \
         patch("app.data.sp500_price_collector._refresh_technicals_bulk"):
        from app.data.sp500_price_collector import collect_sp500_prices

        beat = asyncio.create_task(heartbeat())
        try:
            await collect_sp500_prices(period="5d")
        finally:
            beat.cancel()

    assert ticks >= 5, (
        f"event loop only ticked {ticks}x during a 300ms download — "
        "the collector is blocking the loop again"
    )


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_insert(db_ctx):
    """The ~2,000-row insert loop is the second half of the stall."""
    ctx, db = db_ctx
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_execute(*a, **k):
        # Only the INSERTs are slow; the ticker SELECT must stay fast.
        if a and "INSERT" in str(a[0]):
            time.sleep(0.02)
        cur = MagicMock()
        cur.fetchall.return_value = [("AAA",), ("BBB",)]
        return cur

    db.execute.side_effect = slow_execute

    with patch("app.data.sp500_price_collector.get_db", return_value=ctx), \
         patch("app.data.sp500_price_collector.yf.download",
               return_value=_fake_frame(["AAA", "BBB"])), \
         patch("app.data.sp500_price_collector._refresh_technicals_bulk"):
        from app.data.sp500_price_collector import collect_sp500_prices

        beat = asyncio.create_task(heartbeat())
        try:
            await collect_sp500_prices(period="5d")
        finally:
            beat.cancel()

    assert ticks >= 5, (
        f"event loop only ticked {ticks}x during the insert pass — "
        "the insert loop is blocking again"
    )


@pytest.mark.asyncio
async def test_rows_are_still_written(db_ctx):
    """Offloading must not lose the write — the count and the technicals
    refresh both depend on written_tickers surviving the thread hop."""
    ctx, db = db_ctx

    with patch("app.data.sp500_price_collector.get_db", return_value=ctx), \
         patch("app.data.sp500_price_collector.yf.download",
               return_value=_fake_frame(["AAA", "BBB"])), \
         patch("app.data.sp500_price_collector._refresh_technicals_bulk") as bulk:
        from app.data.sp500_price_collector import collect_sp500_prices

        result = await collect_sp500_prices(period="5d")

    assert result["total"] > 0
    # written_tickers must cross the thread boundary or technicals go stale.
    bulk.assert_called_once()
    assert set(bulk.call_args.args[0]) == {"AAA", "BBB"}


# ── The snapshot path must not read the in-progress bar ──────────────
#
# cycle-v3-1785128960: 7 of 8 tickers stored analysis_price=0.00 while
# price_history held good closes. build_market_snapshot does df.iloc[-1] on
# the frame fetch_ohlcv_dataframe returns; that last row was the NaN
# in-progress session, so `float(latest["Close"]) or None` collapsed to None.
# collect_price_history had already learned to salvage, but this SECOND
# consumer of the same frame had not — hence the fix lives in the fetcher.

@pytest.mark.asyncio
async def test_fetch_ohlcv_drops_the_incomplete_bar():
    """The frame's LAST ROW must be a real session, for every consumer."""
    import pandas as pd
    from unittest.mock import MagicMock, patch

    raw = pd.DataFrame(
        {
            "Open": [100.0, 101.0, float("nan")],
            "High": [105.0, 106.0, float("nan")],
            "Low": [95.0, 96.0, float("nan")],
            "Close": [102.0, 103.0, float("nan")],
            "Volume": [1000, 2000, 2582031],
        },
        index=pd.to_datetime(["2026-07-22", "2026-07-23", "2026-07-24"]),
    )

    inst = MagicMock()
    inst.history.return_value = raw
    with patch("app.collectors.yfinance_collector.yf.Ticker", return_value=inst):
        from app.collectors.yfinance_collector import fetch_ohlcv_dataframe

        df = await fetch_ohlcv_dataframe("BLK", period="30d")

    assert len(df) == 2
    # The exact operation build_market_snapshot performs.
    assert float(df.iloc[-1]["Close"]) == 103.0


@pytest.mark.asyncio
async def test_fetch_ohlcv_returns_none_when_every_bar_is_incomplete():
    import pandas as pd
    from unittest.mock import MagicMock, patch

    raw = pd.DataFrame(
        {"Open": [float("nan")], "High": [float("nan")], "Low": [float("nan")],
         "Close": [float("nan")], "Volume": [123]},
        index=pd.to_datetime(["2026-07-24"]),
    )
    inst = MagicMock()
    inst.history.return_value = raw
    with patch("app.collectors.yfinance_collector.yf.Ticker", return_value=inst):
        from app.collectors.yfinance_collector import fetch_ohlcv_dataframe

        assert await fetch_ohlcv_dataframe("BLK", period="30d") is None
