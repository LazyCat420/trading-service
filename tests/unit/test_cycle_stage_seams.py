"""Contracts at the HANDOFFS between trading-cycle stages.

Every defect found in the 2026-07-26/27 production audit lived in a seam, not
inside a stage. Each stage worked; each pair disagreed about what a value
meant. The unit suite was green through all of them because no unit owns a
handoff.

    collector -> stats      0 rows returned == "ok"                (7e8932a)
    fetcher   -> snapshot   last row is the NaN in-progress bar     (ef5025d)
    baseline  -> prompt     "" for a ticker with no data            (3ebdcf0)
    gate      -> gate       a broad gate masking a specific verdict (3ebdcf0)
    tracker   -> SkillOpt   crashes scored as WIN/LOSS              (6e08766)
    freshness -> horizon    ticker measured against itself          (ef5025d)

These tests assert the CONTRACT each side relies on, using the real functions
and fake data — no DB, no network — so they run in the fast suite.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ═══════════════════════════════════════════════════════════════════════
# SEAM 1: collector -> collector_stats
#   "The collector did not raise" must not be read as "the collector
#   produced data". The whole price path signals failure by RETURN VALUE.
# ═══════════════════════════════════════════════════════════════════════

class TestCollectorToStatsSeam:
    def test_a_zero_return_is_an_error_not_ok(self):
        from app.v3.data_report import _EXPECT_TRUTHY

        assert "yfinance_price" in _EXPECT_TRUTHY
        assert "yfinance_fund" in _EXPECT_TRUTHY

    def test_quiet_news_day_is_not_an_error(self):
        """The counters were made permissive because false alarms trained
        people to ignore them. 0 articles is a quiet day, not a failure —
        widening this set would recreate that problem."""
        from app.v3.data_report import _EXPECT_TRUTHY

        for collector in ("finnhub_news", "reddit", "youtube", "multi_api_news"):
            assert collector not in _EXPECT_TRUTHY

    def test_stats_reconcile_with_the_work_attempted(self):
        """ok + error + skipped must account for every collector run, or the
        summary's own arithmetic is unfalsifiable."""
        from app.v3 import collector_stats

        cid = "cycle-seam-reconcile"
        collector_stats.consume(cid)  # clear
        collector_stats.record(cid, "AAA", ok=["a", "b"], errored=["c"],
                               timed_out=["d"], skipped=["e"])
        agg = collector_stats.consume(cid)

        assert agg["ok"] + agg["error"] + agg["skipped"] + agg["late"] == 5

    def test_late_is_not_folded_into_error(self):
        """A collector that blew the deadline keeps running and lands in the DB
        for the next cycle. Counting it as an error made healthy cycles read as
        mass failure — the reason these counters went permissive."""
        from app.v3 import collector_stats

        cid = "cycle-seam-late"
        collector_stats.consume(cid)
        collector_stats.record(cid, "AAA", ok=[], errored=[], timed_out=["x"], skipped=[])
        agg = collector_stats.consume(cid)

        assert agg["error"] == 0
        assert agg["late"] == 1
        assert agg["failures"] == []


# ═══════════════════════════════════════════════════════════════════════
# SEAM 2: fetch_ohlcv_dataframe -> build_market_snapshot
#   The snapshot does df.iloc[-1]. If the fetcher hands back a frame whose
#   last row is the in-progress NaN bar, price silently becomes None and is
#   stored as analysis_price = 0.00 — the Freshness Gate's next-cycle
#   baseline. 7 of 8 tickers in cycle-v3-1785128960.
# ═══════════════════════════════════════════════════════════════════════

def _frame_with_trailing_nan():
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, float("nan")],
            "High": [105.0, 106.0, float("nan")],
            "Low": [95.0, 96.0, float("nan")],
            "Close": [102.0, 103.0, float("nan")],
            "Volume": [1000, 2000, 2582031],
        },
        index=pd.to_datetime(["2026-07-22", "2026-07-23", "2026-07-24"]),
    )


class TestFetcherToSnapshotSeam:
    @pytest.mark.asyncio
    async def test_last_row_is_always_a_real_session(self):
        inst = MagicMock()
        inst.history.return_value = _frame_with_trailing_nan()
        with patch("app.collectors.yfinance_collector.yf.Ticker", return_value=inst):
            from app.collectors.yfinance_collector import fetch_ohlcv_dataframe

            df = await fetch_ohlcv_dataframe("BLK", period="30d")

        # Exactly what build_market_snapshot does.
        assert float(df.iloc[-1]["Close"]) == 103.0

    @pytest.mark.asyncio
    async def test_the_or_none_collapse_cannot_happen(self):
        """`float(latest.get("Close", 0)) or None` turns NaN into None AND
        turns a legitimate 0.0 into None. Guard the input instead."""
        inst = MagicMock()
        inst.history.return_value = _frame_with_trailing_nan()
        with patch("app.collectors.yfinance_collector.yf.Ticker", return_value=inst):
            from app.collectors.yfinance_collector import fetch_ohlcv_dataframe

            df = await fetch_ohlcv_dataframe("BLK", period="30d")

        price = float(df.iloc[-1].get("Close", 0)) or None
        assert price is not None and price == 103.0

    @pytest.mark.asyncio
    async def test_a_frame_of_only_incomplete_bars_is_none_not_empty(self):
        """None is the documented 'no data' signal; an empty frame would sail
        into df.iloc[-1] and raise instead."""
        raw = pd.DataFrame(
            {"Open": [float("nan")], "High": [float("nan")], "Low": [float("nan")],
             "Close": [float("nan")], "Volume": [1]},
            index=pd.to_datetime(["2026-07-24"]),
        )
        inst = MagicMock()
        inst.history.return_value = raw
        with patch("app.collectors.yfinance_collector.yf.Ticker", return_value=inst):
            from app.collectors.yfinance_collector import fetch_ohlcv_dataframe

            assert await fetch_ohlcv_dataframe("X", period="30d") is None


# ═══════════════════════════════════════════════════════════════════════
# SEAM 3: technical_baseline -> agent prompt
#   The prompt is the only channel through which data quality reaches the
#   model. An empty string says nothing, so the ticker the desk knew least
#   about produced the least warning.
# ═══════════════════════════════════════════════════════════════════════

class TestBaselineToPromptSeam:
    def test_absence_produces_a_louder_block_than_staleness(self):
        from app.quant import technical_baseline as tb

        with patch.object(tb, "compute_technical_baseline", return_value={}):
            missing = tb.build_technical_baseline_block("ASIC")

        assert missing.strip() != ""
        assert "NONE ON FILE" in missing
        assert "data_gaps" in missing

    def test_a_missing_baseline_never_implies_a_level(self):
        """The failure mode is not silence — it is the model inventing levels
        to fill the gap. The block must not hand it a scaffold."""
        from app.quant import technical_baseline as tb

        with patch.object(tb, "compute_technical_baseline", return_value={}):
            block = tb.build_technical_baseline_block("ASIC")

        for token in ("RSI-14:", "SMA-50:", "ATR-14:", "Bollinger position:", "close:"):
            assert token not in block

    def test_stale_block_never_claims_authority(self):
        """CVX was served a 1963-12-26 RSI under 'these are the authoritative
        values'. The model reads the sentence, not the flag."""
        from app.quant import technical_baseline as tb

        stale = {"as_of": "1963-12-26", "stale": True, "age_days": 22856,
                 "age_trading_days": 15000, "rsi": 50.0, "close": 10.0}
        with patch.object(tb, "compute_technical_baseline", return_value=stale):
            block = tb.build_technical_baseline_block("CVX")

        assert "authoritative" not in block.lower()
        assert "STALE" in block

    def test_each_indicator_carries_its_own_freshness(self):
        """One flag over every line is what told the board a 2-day-old close
        was current. Trend and momentum decay at different rates."""
        from app.quant import technical_baseline as tb

        fresh = {"as_of": date(2026, 7, 24), "stale": False, "age_days": 0,
                 "age_trading_days": 0, "close": 202.84, "rsi": 52.1,
                 "sma_200": 206.19, "sma_200_status": "BELOW"}
        with patch.object(tb, "compute_technical_baseline", return_value=fresh):
            block = tb.build_technical_baseline_block("COF")

        assert "[price:" in block
        assert "[momentum:" in block
        assert "[trend:" in block


# ═══════════════════════════════════════════════════════════════════════
# SEAM 4: gate -> gate
#   Gates are ordered, and a broad gate placed early destroys every
#   specific diagnosis behind it. Caught by 32 suite failures when
#   HOLD_NO_PRICE_DATA was first placed before the confidence floor.
# ═══════════════════════════════════════════════════════════════════════

def _executable_desk(ticker="COF", action="BUY", confidence=80):
    from app.v3.shared_desk import SharedDesk

    desk = SharedDesk(ticker=ticker, cycle_id="cycle-seam")
    desk.regime_classification = {"summary": "ok"}
    desk.final_decision = {
        "action": action, "confidence": confidence, "stop_loss": 10.0,
        "dynamic_trigger": {"type": "trailing_drop", "value": 0.1},
        "position_size_pct": 2.0,
    }
    return desk


class TestGateOrderingSeam:
    def test_specific_verdicts_survive_the_broad_data_gate(self):
        """A low-confidence BUY on a ticker with no data must report the
        CONFIDENCE reason. The data gate is last precisely so it cannot
        relabel — and therefore erase — a more informative verdict."""
        import app.quant.technical_baseline as tb
        from app.v3.orchestrator import _apply_policy_gates

        desk = _executable_desk(confidence=40)
        with patch.object(tb, "has_price_history", return_value=False), \
             patch("app.v3.orchestrator._record_gate",
                   side_effect=lambda d, label, **k: label):
            assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"

    def test_the_data_gate_still_blocks_an_otherwise_clean_trade(self):
        import app.quant.technical_baseline as tb
        from app.v3.orchestrator import _apply_policy_gates

        desk = _executable_desk()
        with patch.object(tb, "has_price_history", return_value=False), \
             patch("app.v3.orchestrator._record_gate",
                   side_effect=lambda d, label, **k: label):
            assert _apply_policy_gates(desk) == "HOLD_NO_PRICE_DATA"

    def test_a_probe_outage_cannot_halt_all_trading(self):
        """An unreachable DB answers 'no rows' for EVERY ticker. A data gate
        that failed closed would stop the desk on a Postgres hiccup."""
        import app.quant.technical_baseline as tb
        from app.v3.orchestrator import _apply_policy_gates

        desk = _executable_desk()
        with patch.object(tb, "has_price_history",
                          side_effect=RuntimeError("db down")), \
             patch("app.v3.orchestrator._record_gate",
                   side_effect=lambda d, label, **k: label):
            assert _apply_policy_gates(desk) != "HOLD_NO_PRICE_DATA"

    def test_every_blocking_verdict_is_distinguishable(self):
        """Downstream reads these strings (dashboard label, scorecard,
        no_trade_reason). Two gates sharing a label lose the diagnosis."""
        import app.quant.technical_baseline as tb
        from app.v3.orchestrator import _apply_policy_gates

        seen = {}
        cases = {
            "no_data": (_executable_desk(), False, 80),
            "low_conf": (_executable_desk(confidence=40), True, 40),
        }
        for name, (desk, has_data, _c) in cases.items():
            with patch.object(tb, "has_price_history", return_value=has_data), \
                 patch("app.v3.orchestrator._record_gate",
                       side_effect=lambda d, label, **k: label):
                seen[name] = _apply_policy_gates(desk)

        assert len(set(seen.values())) == len(seen), seen


# ═══════════════════════════════════════════════════════════════════════
# SEAM 5: outcome_tracker -> SkillOpt / scorecard / calibration
#   A crash scored as a trade corrupts every downstream consumer at once.
#   174 pipeline failures were labelled WIN before this was closed.
# ═══════════════════════════════════════════════════════════════════════

class TestTrackerToScoringSeam:
    def test_a_crash_is_never_scored(self):
        from app.autoresearch.outcome_tracker import _is_unscoreable

        crashes = [
            (0, {"action": "BUY"}),
            (45, {"action": "SELL", "thesis_summary": "PIPELINE FAILURE (EMPTY_SIGNAL): x"}),
            (45, {"action": "SELL", "thesis_summary": "Failed to parse thesis. Invalid JSON"}),
            (60, {"action": None}),
        ]
        for conf, artifact in crashes:
            assert _is_unscoreable(conf, artifact) is True, artifact

    def test_a_loser_is_always_scored(self):
        """THE boundary. The confidence floor rests on low-confidence
        decisions being genuinely worse; dropping them destroys the finding
        the whole gate is built on."""
        from app.autoresearch.outcome_tracker import _is_unscoreable

        keepers = [
            (15, {"action": "BUY", "thesis_summary": "Weak but real"}),
            (52, {"action": "HOLD", "thesis_summary": "Waiting"}),
            (74, {"action": "BUY", "thesis_summary": "Strong"}),
        ]
        for conf, artifact in keepers:
            assert _is_unscoreable(conf, artifact) is False, artifact

    def test_the_scorer_and_the_gate_share_one_definition(self):
        """A decision the policy gate refuses to EXECUTE is exactly one the
        scorer must refuse to GRADE. Two definitions would drift."""
        from app.autoresearch.outcome_tracker import _is_unscoreable
        from app.v3.orchestrator import _DEGRADED_PROVENANCE

        for prov in _DEGRADED_PROVENANCE:
            assert _is_unscoreable(70, {"action": "BUY", "decision_provenance": prov}) is True


# ═══════════════════════════════════════════════════════════════════════
# SEAM 6: price_history -> freshness horizon
#   Age must be measured against the market, not against the ticker.
# ═══════════════════════════════════════════════════════════════════════

class TestFreshnessMeasurementSeam:
    def _query_for(self, ticker):
        """The Mongo query `_trading_day_age` counts distinct sessions with.

        The old version scraped SQL text out of a `db.execute` mock; the
        function reads `mongo_store` now, so that mock intercepted nothing.
        Only the helper `mongo_store` ACTUALLY exports is stubbed. Creating a
        stub for a name the module does not have would manufacture the very
        API the production code is missing, and the test would then pass
        against code that cannot run — the result must come back as a real
        count, because `_trading_day_age` swallows its exceptions and returns
        `None`, which would make every assertion below score nothing.
        """
        from app.db import mongo_store
        from app.quant import technical_baseline as tb

        seen = {}

        def _distinct(collection, field, query=None, *a, **k):
            seen["collection"] = collection
            seen["field"] = field
            seen["query"] = query
            return [date(2026, 7, 24), date(2026, 7, 25)]

        with patch.object(mongo_store, "distinct_values", _distinct):
            age = tb._trading_day_age(ticker, date(2026, 7, 27), date(2026, 7, 23))

        assert age == 2, (
            f"the session count never reached the caller (got {age!r}) — the "
            "helper the function calls did not run, so nothing below is "
            "measuring the real query"
        )
        assert seen, "no distinct query was issued"
        return seen

    def test_age_is_never_measured_against_the_ticker_itself(self):
        """'How many of X's bars are newer than X's newest bar?' is 0 by
        construction — so a ticker that STOPPED updating read as current.
        It hit the 15-of-45 names most likely to be stale."""
        query = self._query_for("SWBI")
        assert query["query"].get("ticker") != "SWBI", (
            "age must be counted over the ticker's MARKET, never over the "
            f"ticker itself: {query['query']!r}"
        )

    def test_markets_do_not_contaminate_each_other(self):
        """000660.KS legitimately posts a Monday bar before US open. A single
        US calendar marks every foreign ticker stale on Friday and fresh on
        Sunday."""
        us = self._query_for("SWBI")
        kr = self._query_for("000660.KS")

        # The US peer set is "everything with no market suffix" — the negation
        # that `NOT LIKE '%.%'` used to express.
        assert us["query"]["ticker"] == {"$not": {"$regex": r"\."}}
        # ...and the Korean one is pinned to its own suffix, so the two peer
        # sets are disjoint rather than one contaminating the other.
        assert kr["query"]["ticker"]["$regex"].endswith(r"\.KS$")
        # Both count only sessions in the window under test.
        for q in (us, kr):
            assert q["query"]["date"] == {"$gt": date(2026, 7, 23),
                                          "$lte": date(2026, 7, 27)}
            assert q["collection"] == "price_history"
            assert q["field"] == "date"

    def test_unknown_age_scores_as_suspect_not_current(self):
        from app.quant import technical_baseline as tb

        mult, label = tb._freshness(None, "trend")
        assert mult == 0.3
        assert "unknown" in label

    def test_horizons_reflect_a_45_day_holding_period(self):
        """Realized hold is ~45 days (decision_outcomes, n=2,034). A 2-day-old
        daily bar is a CORRECT input; a horizon that flags it would fire on
        nearly every ticker and be tuned out."""
        from app.quant import technical_baseline as tb

        assert tb._freshness(2, "trend")[1] == "current"
        assert tb._freshness(2, "momentum")[1] == "current"
        # ...but the entry price genuinely does decay in hours.
        assert tb._freshness(2, "price")[0] < 1.0
