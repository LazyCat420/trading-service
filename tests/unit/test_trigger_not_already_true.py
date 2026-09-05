"""A watch for something that is already true is not a watch.

THE LOOP THIS EXISTS FOR — 2026-09-04/05. `cycle-v3-1788565070` HELD SWBI at
58% confidence and armed `sma_50_drop @ 14.42` while the desk's own data report
said:

    Price: $12.89 | SMA_50: $14.42 (price BELOW)

The condition was satisfied at the instant the trigger was written. The checker
fired it on the first sweep past the 30-minute cooldown, spawned an
`edge_case_dynamic` cycle for the same ticker, which HELD again and armed the
same trigger. Three cycles ran in the hour before it was caught (SWBI → LULU →
SWBI), with twelve more already-true triggers queued behind them, each costing
~40-50k tokens per agent run.

The cooldown in `check_triggers` was written against this exact symptom — its
comment says so — and it can only DELAY the first fire. A condition has to be
judged when the watch is set.

WHY THE ROW IS STILL WRITTEN. Refusing outright would delete the desk's stated
condition, which is the failure the normalisation block was added to stop in
2026-08-20. "The desk asked to watch for something that had already happened"
is a fact worth being able to count, so the row is stored with `active: False`
and `inert_reason: 'condition_already_met'`.

The numbers below are the real ones from `price_triggers` / `technicals` /
`price_history`, embedded as fixtures so this test cannot expire.
"""

from __future__ import annotations

import pytest

from app.trading import order_triggers
from app.trading.order_triggers import dynamic_condition_is_met


# (setup, value, price, metric) taken from live rows on 2026-09-05.
ALREADY_TRUE = [
    ("SWBI sma_50_drop", "sma_50_drop", 14.42, 12.89, 14.42),
    ("LULU sma_50_drop", "sma_50_drop", 118.40, 110.53, 118.40),
    ("AMC sma_50_drop", "sma_50_drop", 2.37, 2.65 - 0.40, 2.37),
    ("AVGO sma_200_rise", "sma_200_rise", 369.39, 380.00, 369.39),
]

NOT_YET_TRUE = [
    ("ZS sma_50_drop, price above", "sma_50_drop", 160.96, 169.80, 160.96),
    ("ADBE sma_50_drop, price above", "sma_50_drop", 248.85, 266.51, 248.85),
    ("AVGO sma_200_rise, price below", "sma_200_rise", 369.39, 357.89, 369.39),
    ("C rsi_14_oversold at 30, rsi 52", "rsi_14_oversold", 30.0, 137.72, 52.0),
]


class TestThePredicate:
    @pytest.mark.parametrize("label,setup,value,price,metric", ALREADY_TRUE)
    def test_an_already_satisfied_setup_reads_as_met(
        self, label, setup, value, price, metric
    ):
        met, why = dynamic_condition_is_met(
            setup, value, current_price=price, metric_val=metric
        )
        assert met is True, label
        assert why == ""

    @pytest.mark.parametrize("label,setup,value,price,metric", NOT_YET_TRUE)
    def test_a_real_watch_reads_as_not_met(self, label, setup, value, price, metric):
        met, why = dynamic_condition_is_met(
            setup, value, current_price=price, metric_val=metric
        )
        assert met is False, label
        assert why == ""

    def test_unevaluable_is_reported_separately_from_not_met(self):
        """"Cannot tell" and "not true" are different facts.

        A bare bool would collapse them, and the caller would arm a watch it
        could not judge while believing it had judged it.
        """
        met, why = dynamic_condition_is_met(
            "resistance_breakout", 1.0, current_price=10.0, metric_val=None
        )
        assert met is False
        assert why, "an unevaluable setup must say why"

    def test_a_missing_price_is_not_a_verdict(self):
        met, why = dynamic_condition_is_met(
            "sma_50_drop", 14.42, current_price=None, metric_val=14.42
        )
        assert (met, why) == (False, "")

    def test_a_missing_metric_is_not_a_verdict(self):
        met, why = dynamic_condition_is_met(
            "sma_50_drop", 14.42, current_price=12.89, metric_val=None
        )
        assert (met, why) == (False, "")

    def test_rsi_defaults_match_the_checkers(self):
        """A value of 0 means "unset" and the checker substitutes 30/70. If the
        creation guard used a different default it would judge a different
        condition from the one that will fire."""
        assert dynamic_condition_is_met(
            "rsi_14_oversold", 0.0, current_price=1.0, metric_val=29.0
        )[0] is True
        assert dynamic_condition_is_met(
            "rsi_14_overbought", 0.0, current_price=1.0, metric_val=71.0
        )[0] is True


class TestTheCreationGuard:
    @pytest.fixture
    def store(self, monkeypatch):
        wrote: list = []
        monkeypatch.setattr(order_triggers.mongo_store, "insert_docs",
                            lambda c, d, **kw: wrote.append(d[0]))
        monkeypatch.setattr(order_triggers.mongo_store, "update_docs",
                            lambda *a, **kw: None)
        return wrote

    def _prices(self, monkeypatch, price, metric):
        monkeypatch.setattr(order_triggers, "_get_current_price",
                            lambda t: (price, "fixture"))
        monkeypatch.setattr(order_triggers, "_current_metric",
                            lambda t, col: metric)

    @pytest.mark.asyncio
    async def test_the_swbi_trigger_is_stored_inactive(self, monkeypatch, store):
        """The exact row that started the loop."""
        self._prices(monkeypatch, 12.89, 14.42)
        out = await order_triggers.create_trigger(
            bot_id="b", ticker="SWBI", trigger_type="dynamic", trigger_price=0.0,
            action="BUY", dynamic_trigger_type="sma_50_drop",
            dynamic_trigger_value=14.42, created_by="pipeline",
        )
        assert "error" not in out
        assert store, "the desk's condition must still be recorded"
        assert store[0]["active"] is False
        assert store[0]["inert_reason"] == "condition_already_met"
        assert out["active"] is False

    @pytest.mark.asyncio
    async def test_a_real_watch_still_arms(self, monkeypatch, store):
        """The control. Without this the guard could be disarming everything."""
        self._prices(monkeypatch, 169.80, 160.96)
        await order_triggers.create_trigger(
            bot_id="b", ticker="ZS", trigger_type="dynamic", trigger_price=0.0,
            action="BUY", dynamic_trigger_type="sma_50_drop",
            dynamic_trigger_value=160.96, created_by="pipeline",
        )
        assert store[0]["active"] is True
        assert store[0]["inert_reason"] is None

    @pytest.mark.asyncio
    async def test_an_unreadable_price_arms_rather_than_blocks(
        self, monkeypatch, store
    ):
        """Fail OPEN. A store outage is not evidence the condition is true, and
        it must not stop the desk recording its condition — the armed trigger is
        then no worse than the pre-fix behaviour."""
        from pymongo.errors import PyMongoError

        def _boom(_t):
            raise PyMongoError("no primary")

        monkeypatch.setattr(order_triggers, "_get_current_price", _boom)
        monkeypatch.setattr(order_triggers, "_current_metric", lambda t, c: 14.42)
        await order_triggers.create_trigger(
            bot_id="b", ticker="SWBI", trigger_type="dynamic", trigger_price=0.0,
            action="BUY", dynamic_trigger_type="sma_50_drop",
            dynamic_trigger_value=14.42, created_by="pipeline",
        )
        assert store[0]["active"] is True

    @pytest.mark.asyncio
    async def test_a_stop_loss_is_untouched(self, monkeypatch, store):
        """The guard is scoped to dynamic setups. A stop-loss below the current
        price is a NORMAL stop-loss, not an already-true watch, and disarming
        those would remove the book's protection."""
        self._prices(monkeypatch, 100.0, None)
        await order_triggers.create_trigger(
            bot_id="b", ticker="AAPL", trigger_type="stop_loss",
            trigger_price=90.0, action="SELL", created_by="pipeline",
        )
        assert store[0]["active"] is True
        assert store[0]["inert_reason"] is None


class TestTheCheckerAndTheGuardCannotDrift:
    def test_the_checker_evaluates_through_the_shared_predicate(self):
        """Both sides must ask the same question.

        A creation guard with its own copy of the comparison would arm watches
        the checker fires immediately — the defect this was extracted to fix,
        reintroduced one refactor later.
        """
        import inspect

        src = inspect.getsource(order_triggers.check_triggers)
        assert "dynamic_condition_is_met(" in src

    def test_the_creation_path_evaluates_through_it_too(self):
        import inspect

        src = inspect.getsource(order_triggers.create_trigger)
        assert "dynamic_condition_is_met(" in src

    def test_the_checker_holds_no_second_comparison(self):
        """The inline `current_price < metric_val` arithmetic must be GONE from
        the checker, not merely unused beside the call."""
        import inspect

        src = inspect.getsource(order_triggers.check_triggers)
        assert "current_price < metric_val" not in src
        assert "current_price > metric_val" not in src
