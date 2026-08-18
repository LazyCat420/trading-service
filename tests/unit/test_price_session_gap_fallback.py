"""A partial fetch must not suppress the provider that can complete it.

Probed on the live container 2026-07-27 (a Monday): yfinance served Friday
2026-07-24 as NaN OHLC with a real Volume for both ASC and SBUX, three days
after that session closed. `fetch_ohlcv_dataframe` correctly drops such a bar,
so `collect_price_history` returned 250 good rows and a stale tip.

`fetch_price_history` then returned on `count > 0`, so FMP and Polygon were
never asked — and 250 is very much > 0. The result was 509 tickers current to
07-24 (the S&P 500 post-close loop caught the bar while it was briefly
complete) against 71 frozen at 07-23, including AGNC, ASC and BOOT: three of
the seven tickers cycle-v3-1785137616 actually traded on.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.collectors import data_rotator


@pytest.fixture
def _no_fmp_or_polygon():
    with patch.object(data_rotator.settings, "FMP_API_KEY", ""), \
         patch.object(data_rotator.settings, "POLYGON_API_KEY", ""), \
         patch.object(data_rotator.settings, "MASSIVE_API_KEY", ""):
        yield


class TestSessionGapTriggersFallback:
    @pytest.mark.asyncio
    async def test_fresh_yfinance_result_short_circuits(self, _no_fmp_or_polygon):
        """The happy path must not pay for a gap probe twice or refetch."""
        with patch.object(data_rotator.yfinance_collector, "collect_price_history",
                          AsyncMock(return_value=250)), \
             patch.object(data_rotator, "_is_missing_recent_session", return_value=False):
            assert await data_rotator.fetch_price_history("SBUX") == 250

    @pytest.mark.asyncio
    async def test_stale_tip_falls_through_to_polygon(self):
        """250 rows with a missing newest session must still reach Polygon."""
        poly = AsyncMock(return_value=250)
        with patch.object(data_rotator.yfinance_collector, "collect_price_history",
                          AsyncMock(return_value=250)), \
             patch.object(data_rotator, "_is_missing_recent_session", return_value=True), \
             patch.object(data_rotator.settings, "FMP_API_KEY", ""), \
             patch.object(data_rotator.settings, "POLYGON_API_KEY", "k"), \
             patch.object(data_rotator.settings, "MASSIVE_API_KEY", ""), \
             patch.object(data_rotator.polygon_collector, "collect_price_history", poly):
            total = await data_rotator.fetch_price_history("ASC")

        poly.assert_awaited_once()
        assert total == 500

    @pytest.mark.asyncio
    async def test_yfinance_rows_survive_a_useless_fallback(self):
        """A fallback that adds nothing must not zero the return value.

        Callers treat 0 as a total outage, and _EXPECT_TRUTHY turns it into a
        recorded collector error — so erasing yfinance's 250 rows would
        manufacture a false failure.
        """
        with patch.object(data_rotator.yfinance_collector, "collect_price_history",
                          AsyncMock(return_value=250)), \
             patch.object(data_rotator, "_is_missing_recent_session", return_value=True), \
             patch.object(data_rotator.settings, "FMP_API_KEY", ""), \
             patch.object(data_rotator.settings, "POLYGON_API_KEY", "k"), \
             patch.object(data_rotator.settings, "MASSIVE_API_KEY", ""), \
             patch.object(data_rotator.polygon_collector, "collect_price_history",
                          AsyncMock(return_value=0)):
            assert await data_rotator.fetch_price_history("ASC") == 250

    @pytest.mark.asyncio
    async def test_polygon_exception_does_not_lose_yfinance_rows(self):
        with patch.object(data_rotator.yfinance_collector, "collect_price_history",
                          AsyncMock(return_value=250)), \
             patch.object(data_rotator, "_is_missing_recent_session", return_value=True), \
             patch.object(data_rotator.settings, "FMP_API_KEY", ""), \
             patch.object(data_rotator.settings, "POLYGON_API_KEY", "k"), \
             patch.object(data_rotator.settings, "MASSIVE_API_KEY", ""), \
             patch.object(data_rotator.polygon_collector, "collect_price_history",
                          AsyncMock(side_effect=RuntimeError("429"))):
            assert await data_rotator.fetch_price_history("ASC") == 250

    @pytest.mark.asyncio
    async def test_total_outage_still_reports_zero(self, _no_fmp_or_polygon):
        with patch.object(data_rotator.yfinance_collector, "collect_price_history",
                          AsyncMock(return_value=0)):
            assert await data_rotator.fetch_price_history("ASC") == 0


class TestGapProbe:
    def test_probe_fails_closed(self):
        """An unreachable DB must not make every ticker look stale and set off
        fallback fetches fleet-wide."""
        with patch("app.db.connection.get_db", side_effect=RuntimeError("down")):
            assert data_rotator._is_missing_recent_session("ASC") is False

    def test_ticker_with_no_rows_is_not_a_gap(self):
        """No history at all is step 1's problem, not a missing-session one."""
        class _Cur:
            def execute(self, *a, **k):
                return self

            def fetchone(self):
                return (None,)

        class _Ctx:
            def __enter__(self):
                return _Cur()

            def __exit__(self, *a):
                return False

        with patch("app.db.connection.get_db", return_value=_Ctx()):
            assert data_rotator._is_missing_recent_session("NEWTICKER") is False


class TestPolygonKeyLookup:
    """Polygon served news while its PRICE fallback was unreachable."""

    def test_massive_api_key_is_accepted(self):
        from app.collectors import polygon_collector
        with patch.object(polygon_collector.settings, "POLYGON_API_KEY", ""), \
             patch.object(polygon_collector.settings, "MASSIVE_API_KEY", "m-key"):
            assert polygon_collector._get_key() == "m-key"

    def test_polygon_api_key_still_wins(self):
        from app.collectors import polygon_collector
        with patch.object(polygon_collector.settings, "POLYGON_API_KEY", "p-key"), \
             patch.object(polygon_collector.settings, "MASSIVE_API_KEY", "m-key"):
            assert polygon_collector._get_key() == "p-key"

    def test_no_key_at_all_still_raises(self):
        from app.collectors import polygon_collector
        with patch.object(polygon_collector.settings, "POLYGON_API_KEY", ""), \
             patch.object(polygon_collector.settings, "MASSIVE_API_KEY", ""):
            with pytest.raises(ValueError):
                polygon_collector._get_key()

    def test_rotator_gate_matches_the_collector(self):
        """The gate and the key lookup must agree, or we either skip a usable
        provider or call one that raises immediately."""
        import inspect
        src = inspect.getsource(data_rotator.fetch_price_history)
        assert "MASSIVE_API_KEY" in src
