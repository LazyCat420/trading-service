"""Drive each cycle stage through the failures that actually happened.

The 2026-07-26 outage was not caught by 1,381 passing tests because every test
fed its unit a HEALTHY input. The bugs only appeared when a stage was handed
something broken and passed it on wearing a success label.

So: inject the real fault, assert the real consequence. Each class names the
production incident it reproduces, and each test asserts what the system must
do — not what it currently happens to do.

No DB, no network. These belong in the fast suite because a battle test nobody
runs is not a battle test.
"""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ═══════════════════════════════════════════════════════════════════════
# FAULT: the vendor returns a frame whose newest bar is incomplete.
#   Reality, not a hypothetical — every yfinance frame carries one, all day,
#   every day. It cost 124 of 125 rows per ticker until 7e8932a.
# ═══════════════════════════════════════════════════════════════════════

def _vendor_frame(n_good=124, trailing_nan=True):
    rows = n_good + (1 if trailing_nan else 0)
    idx = pd.date_range("2026-01-01", periods=rows, freq="D")
    good = list(range(100, 100 + n_good))
    o = [float(v) for v in good] + ([float("nan")] if trailing_nan else [])
    return pd.DataFrame(
        {"Open": o, "High": o, "Low": o, "Close": o,
         "Volume": [1000] * n_good + ([2582031] if trailing_nan else [])},
        index=idx,
    )


class TestIncompleteBarFault:
    @pytest.mark.asyncio
    async def test_one_bad_bar_does_not_cost_the_other_124(self, monkeypatch):
        from app.collectors import yfinance_collector as yc

        inst = MagicMock()
        inst.history.return_value = _vendor_frame()
        monkeypatch.setattr(yc.yf, "Ticker", lambda t: inst)
        monkeypatch.setattr(yc, "_refresh_technicals", _anoop)

        db, ctx = _fake_db()
        monkeypatch.setattr(yc, "get_db", lambda: ctx)

        count = await yc.collect_price_history("BLK")
        assert count == 124, "the salvage regressed — one NaN row killed the frame"

    @pytest.mark.asyncio
    async def test_a_narrower_fetch_window_does_not_dodge_it(self, monkeypatch):
        """The trap that wasted a plan revision: the NaN is in the NEWEST bar,
        so `period="5d"` contains it just as reliably as `6mo`. Salvage is the
        fix; a smaller window is not."""
        from app.collectors import yfinance_collector as yc

        inst = MagicMock()
        inst.history.return_value = _vendor_frame(n_good=4)
        monkeypatch.setattr(yc.yf, "Ticker", lambda t: inst)

        df = await yc.fetch_ohlcv_dataframe("BLK", period="5d")
        assert not df["Close"].isna().any()

    @pytest.mark.asyncio
    async def test_a_totally_empty_vendor_response_is_not_a_silent_zero(
        self, monkeypatch
    ):
        from app.collectors import yfinance_collector as yc

        inst = MagicMock()
        inst.history.return_value = _vendor_frame(n_good=0)
        monkeypatch.setattr(yc.yf, "Ticker", lambda t: inst)
        monkeypatch.setattr(yc, "_refresh_technicals", _anoop)
        _db, ctx = _fake_db()
        monkeypatch.setattr(yc, "get_db", lambda: ctx)

        assert await yc.collect_price_history("BLK") == 0


# ═══════════════════════════════════════════════════════════════════════
# FAULT: every price provider is down.
#   The cycle must not report a clean collector record. This is the exact
#   2026-07-26 failure: 12/12 tickers lost every provider, summary said
#   collector_ok=49, collector_error=0, collector_failures=[].
# ═══════════════════════════════════════════════════════════════════════

class TestTotalProviderOutage:
    def test_a_zero_row_return_is_recorded_as_an_error(self):
        from app.v3 import collector_stats
        from app.v3.data_report import _EXPECT_TRUTHY

        # The decision rule the wrapper applies.
        assert not _is_ok("yfinance_price", 0, _EXPECT_TRUTHY)
        assert not _is_ok("yfinance_fund", False, _EXPECT_TRUTHY)
        assert _is_ok("yfinance_price", 124, _EXPECT_TRUTHY)

    def test_an_outage_leaves_a_named_failure_for_every_error(self):
        """collector_failures must name what broke. An error count with an
        empty list is unactionable — and is what shipped for months."""
        from app.v3 import collector_stats

        cid = "cycle-fault-outage"
        collector_stats.consume(cid)
        for tk in ("AAA", "BBB", "CCC"):
            collector_stats.record(cid, tk, ok=[], errored=["yfinance_price"],
                                   timed_out=[], skipped=[])
        agg = collector_stats.consume(cid)

        assert agg["error"] == 3
        assert len(agg["failures"]) == 3
        assert all("yfinance_price" in f for f in agg["failures"])

    def test_a_slow_collector_is_not_an_outage(self):
        """The counter-fault. Over-reporting is how the previous telemetry
        got tuned into permissiveness — late work still lands next cycle."""
        from app.v3 import collector_stats

        cid = "cycle-fault-slow"
        collector_stats.consume(cid)
        collector_stats.record(cid, "AAA", ok=[], errored=[],
                               timed_out=["reddit", "youtube"], skipped=[])
        agg = collector_stats.consume(cid)

        assert agg["error"] == 0 and agg["late"] == 2


# ═══════════════════════════════════════════════════════════════════════
# FAULT: a ticker with no price history reaches the desk.
#   ASIC: zero rows, full panel, BUY @ 68. Its own risk_flags named the gap
#   and the confidence did not move.
# ═══════════════════════════════════════════════════════════════════════

def _desk(ticker="ASIC", action="BUY", confidence=80):
    from app.v3.shared_desk import SharedDesk

    d = SharedDesk(ticker=ticker, cycle_id="cycle-fault")
    d.regime_classification = {"summary": "ok"}
    d.final_decision = {
        "action": action, "confidence": confidence, "stop_loss": 10.0,
        "dynamic_trigger": {"type": "trailing_drop", "value": 0.1},
        "position_size_pct": 2.0,
    }
    return d


class TestDatalessTickerFault:
    def test_a_dataless_ticker_cannot_reach_execution(self):
        import app.quant.technical_baseline as tb
        from app.v3.orchestrator import _apply_policy_gates

        with patch.object(tb, "has_price_history", return_value=False), \
             patch("app.v3.orchestrator._record_gate",
                   side_effect=lambda d, l, **k: l):
            assert _apply_policy_gates(_desk()) == "HOLD_NO_PRICE_DATA"

    def test_the_prompt_says_so_out_loud(self):
        """The gate is the backstop; the prompt is the fix. The model must be
        told the anchor is missing rather than left to infer levels."""
        from app.quant import technical_baseline as tb

        with patch.object(tb, "compute_technical_baseline", return_value={}):
            block = tb.build_technical_baseline_block("ASIC")
        assert "NONE ON FILE" in block

    def test_a_healthy_ticker_is_unaffected(self):
        """A gate that blocks everything is not a gate."""
        import app.quant.technical_baseline as tb
        from app.v3.orchestrator import _apply_policy_gates

        with patch.object(tb, "has_price_history", return_value=True), \
             patch("app.v3.orchestrator._record_gate",
                   side_effect=lambda d, l, **k: l):
            assert _apply_policy_gates(_desk(ticker="COF")) == "EXECUTE_BUY"


# ═══════════════════════════════════════════════════════════════════════
# FAULT: the agent panel crashes and emits a degraded artifact.
#   174 such crashes were scored WIN before 6e08766.
# ═══════════════════════════════════════════════════════════════════════

class TestDegradedArtifactFault:
    @pytest.mark.parametrize("artifact", [
        {"action": "BUY", "confidence": 0},
        {"action": "SELL", "thesis_summary": "PIPELINE FAILURE (EMPTY_SIGNAL): 0 claims"},
        {"action": "SELL", "thesis_summary": "Failed to parse thesis. Invalid JSON format."},
        {"action": None, "confidence": 60},
    ])
    def test_a_crash_never_becomes_a_scored_trade(self, artifact):
        from app.autoresearch.outcome_tracker import _is_unscoreable

        conf = artifact.get("confidence", 50)
        assert _is_unscoreable(conf, artifact) is True

    def test_the_policy_gate_refuses_it_too(self):
        """Gate and scorer must agree — a decision too broken to execute is
        too broken to grade."""
        from app.v3.orchestrator import _apply_policy_gates, _DEGRADED_PROVENANCE

        d = _desk()
        d.final_decision = {"action": "BUY", "confidence": 80,
                            "decision_provenance": next(iter(_DEGRADED_PROVENANCE))}
        with patch("app.v3.orchestrator._record_gate",
                   side_effect=lambda dk, l, **k: l):
            assert _apply_policy_gates(d) == "HOLD_DEGRADED_NO_DECISION"

    def test_a_real_losing_trade_is_still_scored(self):
        """The boundary. Filtering losers instead of failures would delete the
        evidence the confidence floor is built on."""
        from app.autoresearch.outcome_tracker import _is_unscoreable

        assert _is_unscoreable(15, {"action": "BUY",
                                    "thesis_summary": "Thin setup, low conviction"}) is False


# ═══════════════════════════════════════════════════════════════════════
# FAULT: a ticker silently stops receiving bars.
#   SWBI sat a session behind 510 peers and read as `current`.
# ═══════════════════════════════════════════════════════════════════════

class TestStalledTickerFault:
    def test_a_stalled_ticker_does_not_report_itself_current(self):
        from app.quant import technical_baseline as tb

        captured = {}

        def _exec(sql, params=None):
            captured["sql"] = " ".join(sql.split())
            cur = MagicMock()
            cur.fetchone.return_value = (1,)   # one peer session has passed
            return cur

        db = MagicMock()
        db.execute.side_effect = _exec
        ctx = MagicMock()
        ctx.__enter__.return_value = db

        with patch("app.db.connection.get_db", return_value=ctx):
            age = tb._trading_day_age("SWBI", date(2026, 7, 27), date(2026, 7, 23))

        assert age == 1
        assert "ticker = %s" not in captured["sql"], (
            "self-referential age is 0 by construction for a stalled ticker"
        )

    def test_a_foreign_market_is_not_judged_by_the_us_calendar(self):
        from app.quant import technical_baseline as tb

        captured = {}

        def _exec(sql, params=None):
            captured["params"] = params
            cur = MagicMock()
            cur.fetchone.return_value = (0,)
            return cur

        db = MagicMock()
        db.execute.side_effect = _exec
        ctx = MagicMock()
        ctx.__enter__.return_value = db

        with patch("app.db.connection.get_db", return_value=ctx):
            tb._trading_day_age("000660.KS", date(2026, 7, 27), date(2026, 7, 27))

        assert captured["params"]["pat"] == "%.KS"


# ═══════════════════════════════════════════════════════════════════════
# FAULT: a blocking call lands on the event loop during boot.
#   Every deploy went UNHEALTHY for ~2 min at 23% CPU.
# ═══════════════════════════════════════════════════════════════════════

class TestEventLoopStarvationFault:
    @pytest.mark.asyncio
    async def test_the_loop_survives_a_slow_bulk_refresh(self, monkeypatch):
        import time as _t

        from app.data import sp500_price_collector as sp

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        idx = pd.to_datetime(["2026-07-23", "2026-07-24"])
        cols = pd.MultiIndex.from_product(
            [["AAA"], ["Open", "High", "Low", "Close", "Volume"]])
        frame = pd.DataFrame([[1.0, 2.0, 0.5, 1.5, 10]] * 2, index=idx, columns=cols)

        def slow(*a, **k):
            _t.sleep(0.25)
            return frame

        _db, ctx = _fake_db(fetchall=[("AAA",)])
        monkeypatch.setattr(sp, "get_db", lambda: ctx)
        monkeypatch.setattr(sp.yf, "download", slow)
        monkeypatch.setattr(sp, "_refresh_technicals_bulk", _anoop_list)

        beat = asyncio.create_task(heartbeat())
        try:
            await sp.collect_sp500_prices(period="5d")
        finally:
            beat.cancel()

        assert ticks >= 5, (
            f"loop ticked only {ticks}x — a blocking call is back on the loop, "
            "and /health will time out during boot"
        )


# ── helpers ─────────────────────────────────────────────────────────────

async def _anoop(*a, **k):
    return None


async def _anoop_list(*a, **k):
    return None


def _is_ok(name, result, expect_truthy) -> bool:
    """The wrapper's decision rule, mirrored so the fault tests exercise the
    real constant rather than a copy of the list."""
    return not (name in expect_truthy and not result)


def _fake_db(fetchall=None):
    db = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = fetchall if fetchall is not None else []
    cur.fetchone.return_value = None
    db.execute.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = db
    return db, ctx
