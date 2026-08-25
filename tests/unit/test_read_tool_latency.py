"""A READ tool must answer within its own deadline.

MEASURED 2026-08-25 (7d of agent_tool_telemetry): get_market_data p50 20.9s /
p95 36.0s against a 30s bridge deadline (24% aborted); get_finnhub_news p50
36.2s / p95 65.2s against 60s (16% aborted). Both ran their COLLECTION path on
every call before reading a row. The dominant cost was a single unindexed
`distinct` over 15.77M price_history docs (27.9s, full scan) inside
`_trading_day_age`. After the fix: get_market_data 0.1s, get_finnhub_news
0.2-8s depending on store freshness.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.tools.read_through import (
    age_hours,
    refresh_within_budget,
    store_can_answer,
)


class TestStoreCanAnswer:
    def test_fresh_rows_answer_without_the_network(self):
        newest = datetime.now(timezone.utc) - timedelta(hours=1)
        assert store_can_answer(newest, max_age_h=6.0) is True

    def test_stale_rows_force_a_refresh(self):
        newest = datetime.now(timezone.utc) - timedelta(hours=48)
        assert store_can_answer(newest, max_age_h=6.0) is False

    def test_no_rows_force_a_refresh(self):
        assert store_can_answer(None, max_age_h=6.0) is False
        assert store_can_answer(datetime.now(timezone.utc), 6.0, have_rows=False) is False

    def test_naive_datetimes_are_tolerated(self):
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        assert store_can_answer(naive, max_age_h=6.0) is True
        assert age_hours(naive) == pytest.approx(1.0, abs=0.2)


class TestRefreshWithinBudget:
    @pytest.mark.asyncio
    async def test_a_fast_refresh_completes(self):
        ran = []

        async def quick():
            ran.append(1)

        assert await refresh_within_budget("t", quick, budget_s=5.0) is True
        assert ran == [1]

    @pytest.mark.asyncio
    async def test_a_slow_refresh_is_abandoned_not_awaited(self):
        """The whole point: a stale answer beats an aborted one."""
        import asyncio, time

        async def slow():
            await asyncio.sleep(30)

        t0 = time.monotonic()
        ok = await refresh_within_budget("t", slow, budget_s=0.2)
        assert ok is False
        assert time.monotonic() - t0 < 2.0

    @pytest.mark.asyncio
    async def test_a_failing_refresh_never_raises(self):
        async def boom():
            raise RuntimeError("provider down")

        assert await refresh_within_budget("t", boom, budget_s=5.0) is False


class TestPeerSessionProbeIsMemoised:
    """`_trading_day_age`'s peer query is per-MARKET: every US ticker sharing a
    `latest` date asks the identical question. Pre-fix, each call paid the full
    27.9s collection scan again. This test is RED on the pre-fix module (the
    stub below counts 3 distinct_values calls there; the fix makes it 1)."""

    def test_identical_market_questions_hit_the_store_once(self, monkeypatch):
        from datetime import date
        import app.quant.technical_baseline as tb

        getattr(tb, "_PEER_SESSION_CACHE", {}).clear()   # absent pre-fix: runnable both ways
        calls = []
        monkeypatch.setattr(
            __import__("app.db.mongo_store", fromlist=["x"]),
            "distinct_values",
            lambda coll, field, q: calls.append(q) or [],
        )
        for _ in range(3):
            tb._trading_day_age("AAPL", date(2026, 8, 25), date(2026, 8, 24))
        assert len(calls) == 1, f"expected 1 store hit for 3 identical probes, got {len(calls)}"

    def test_different_markets_do_not_share_an_answer(self, monkeypatch):
        from datetime import date
        import app.quant.technical_baseline as tb

        getattr(tb, "_PEER_SESSION_CACHE", {}).clear()
        calls = []
        monkeypatch.setattr(
            __import__("app.db.mongo_store", fromlist=["x"]),
            "distinct_values",
            lambda coll, field, q: calls.append(q) or [],
        )
        tb._trading_day_age("AAPL", date(2026, 8, 25), date(2026, 8, 24))
        tb._trading_day_age("000660.KS", date(2026, 8, 25), date(2026, 8, 24))
        assert len(calls) == 2

    def test_the_date_index_is_declared_where_reseeds_rebuild_it(self):
        """The index was created live, but an index that lives only in the
        server dies with the next backfill ([[the-collection-nothing-seeds-has-
        no-index]]). It must be declared in ensure_indexes."""
        import inspect
        from app.db import mongo_store

        src = inspect.getsource(mongo_store.ensure_indexes)
        assert '"price_history"' in src and '"date_1"' in src
