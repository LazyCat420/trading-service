"""Two defects found by the 2026-08-09 5-ticker debug cycle.

1. MGNI was dropped at cycle start for "no usable price history" while its own
   6-month backfill landed minutes later — a fresh gatekeeper pick is probed
   BEFORE anything has ever collected its prices. The pre-flight must attempt
   the backfill itself and only drop a ticker that stays empty.

2. The drop event never reached the UI: a function-local
   `from datetime import datetime` in `PipelineStateDB.append_events` shadowed
   the module import for the whole scope, so any event WITHOUT a "ts" key
   raised "cannot access free variable 'datetime'" before a row was built.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.pipeline_service import PipelineService


# ── 1. pre-flight backfill ──────────────────────────────────────────────


class TestPreflightPriceHistory:
    @pytest.mark.asyncio
    async def test_a_ticker_with_history_is_not_backfilled(self):
        backfill = MagicMock()
        with patch("app.quant.technical_baseline.has_price_history",
                   return_value=True), \
             patch("app.collectors.yfinance_collector.collect_price_history",
                   backfill):
            assert await PipelineService._preflight_price_history("ALLY") is True
        backfill.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_fresh_pick_is_backfilled_then_kept(self):
        """The MGNI case: empty at probe time, rescued by its own backfill."""
        async def fake_backfill(ticker, period="6mo"):
            return 125

        with patch("app.quant.technical_baseline.has_price_history",
                   side_effect=[False, True]) as probe, \
             patch("app.collectors.yfinance_collector.collect_price_history",
                   side_effect=fake_backfill):
            assert await PipelineService._preflight_price_history("MGNI") is True
        assert probe.call_count == 2

    @pytest.mark.asyncio
    async def test_a_genuinely_empty_ticker_is_still_dropped(self):
        async def fake_backfill(ticker, period="6mo"):
            return 0

        with patch("app.quant.technical_baseline.has_price_history",
                   return_value=False), \
             patch("app.collectors.yfinance_collector.collect_price_history",
                   side_effect=fake_backfill):
            assert await PipelineService._preflight_price_history("GHOST") is False

    @pytest.mark.asyncio
    async def test_a_failing_backfill_does_not_mask_the_drop(self):
        async def broken_backfill(ticker, period="6mo"):
            raise RuntimeError("vendor down")

        with patch("app.quant.technical_baseline.has_price_history",
                   return_value=False), \
             patch("app.collectors.yfinance_collector.collect_price_history",
                   side_effect=broken_backfill):
            assert await PipelineService._preflight_price_history("GHOST") is False


# ── 2. ts-less events must still append ─────────────────────────────────


class TestTslessEventAppend:
    def test_an_event_without_ts_is_written_not_swallowed(self):
        from app.services import pipeline_state as ps

        captured = {}

        class FakeDB:
            def executemany(self, sql, rows):
                captured["rows"] = rows

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(ps.connection, "get_db", return_value=FakeDB()), \
             patch("app.db.mongo_store.writes_mongo", return_value=False):
            ps.PipelineStateDB.append_events("cycle-test", [{
                "phase": "analyzing",
                "step": "v3_dropped_MGNI",
                "status": "skipped",
                "detail": "dropped before analysis",
                "data": {"reason": "no_price_history"},
            }])

        assert "rows" in captured and len(captured["rows"]) == 1
        # The fallback timestamp must be a real value, not an unraised name.
        assert captured["rows"][0][2] is not None
