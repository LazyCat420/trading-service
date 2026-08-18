"""
Dynamic trigger evaluability (2026-08-10 cycle audit).

Two defects, one root: `dynamic_trigger_type` is free text an agent writes,
and nothing checked whether the monitor could actually read it.

  * `sma_100_drop` reached `SELECT sma_100 FROM technicals` — a column that has
    never existed — and logged 319 errors in five hours for one ACHR row.
  * 68 of 147 active dynamic triggers (46%) named a setup the comparison
    ladder matches nothing in, so they sat active and silently never fired.

Only the first was visible, because only the first raised anything.
"""

import logging

import pytest

from app.trading.order_triggers import (
    _TRIGGER_METRIC_COLUMNS,
    dynamic_trigger_is_evaluable,
)
from app.v3.artifact_validators import validate_trade_decision_artifact


# ── What the monitor can actually evaluate ─────────────────────────────

@pytest.mark.parametrize("setup", [
    "sma_20_drop", "sma_50_drop", "sma_200_drop",
    "sma_20_cross_above", "sma_50_cross_above",
    "rsi_14_oversold", "rsi_14_overbought",
    "trailing_drop",
])
def test_setups_that_fire_today_keep_firing(setup):
    """Behaviour lock. These 78 of 147 active rows work; a stricter registry
    that silently dropped one would disable a live watch."""
    assert dynamic_trigger_is_evaluable(setup) is True


@pytest.mark.parametrize("setup,why", [
    ("sma_100_drop", "no sma_100 column — this is the one that logged 319 errors"),
    ("sma_50_reclaim", "18 active rows; 'reclaim' matches no direction"),
    ("sma_50_breakout", "7 active rows"),
    ("sma_200_break", "4 active rows"),
    ("sma_50_cross", "no direction word"),
    ("sma_50_resistance", "no direction word"),
    ("support_retest", "never enters the sma_/rsi_ branch at all"),
    ("resistance_breakout", "never enters the branch"),
    ("buy_at_support", "never enters the branch"),
    ("price_cross_above", "never enters the branch"),
    ("rsi_14_drop", "rsi setup naming neither oversold nor overbought"),
    ("", "empty"),
])
def test_inert_setups_are_reported_as_unevaluatable(setup, why):
    assert dynamic_trigger_is_evaluable(setup) is False, why


def test_only_real_technicals_columns_are_reachable():
    """The whitelist is the only thing that can name a column in that SQL."""
    assert _TRIGGER_METRIC_COLUMNS == {"sma_20", "sma_50", "sma_200", "rsi_14"}
    assert "sma_100" not in _TRIGGER_METRIC_COLUMNS


def test_a_setup_cannot_smuggle_sql_through_the_type():
    assert dynamic_trigger_is_evaluable("sma_20; DROP TABLE technicals--") is False


# ── The validator must stop minting dead rows ──────────────────────────

def test_unevaluatable_setup_is_dropped_even_when_it_carries_a_value():
    """The leak that produced 68 inert rows.

    The validator already dropped unknown setups whose `value` was missing,
    for exactly this reason — but agents usually DO attach a price level, and
    those sailed through and registered as active triggers that never evaluate.
    """
    a = validate_trade_decision_artifact(
        {"action": "HOLD", "dynamic_trigger": {"type": "support_retest", "value": 206.72}}
    )
    assert a["dynamic_trigger"] is None
    assert any("cannot be evaluated" in n for n in a.get("_validator_notes", []))


def test_sma_100_drop_never_becomes_a_trigger_row():
    a = validate_trade_decision_artifact(
        {"action": "HOLD", "dynamic_trigger": {"type": "sma_100_drop", "value": 5.22}}
    )
    assert a["dynamic_trigger"] is None


def test_a_supported_setup_with_a_value_is_untouched():
    a = validate_trade_decision_artifact(
        {"action": "HOLD", "dynamic_trigger": {"type": "sma_50_drop", "value": 145.5}}
    )
    assert a["dynamic_trigger"] == {"type": "sma_50_drop", "value": 145.5}


def test_supported_setup_missing_its_value_still_gets_the_placeholder():
    a = validate_trade_decision_artifact(
        {"action": "HOLD", "dynamic_trigger": {"type": "sma_50_drop", "value": None}}
    )
    assert a["dynamic_trigger"]["value"] == 0.0


def test_rsi_default_threshold_survives():
    a = validate_trade_decision_artifact(
        {"action": "HOLD", "dynamic_trigger": {"type": "rsi_14_oversold", "value": None}}
    )
    assert a["dynamic_trigger"]["value"] == 30.0


# ── The error storm must not become a warning storm ────────────────────

def test_an_inert_trigger_is_reported_once_not_once_per_scheduler_pass(caplog):
    """The checker runs every 60s over ~147 active rows. Logging per pass
    would replace 1,440 errors/day with 1,440 warnings/day."""
    from app.trading import order_triggers

    order_triggers._INERT_TRIGGERS_SEEN.discard("trg-test-once")
    with caplog.at_level(logging.WARNING, logger="app.trading.order_triggers"):
        for _ in range(5):
            order_triggers._note_inert_trigger(
                "trg-test-once", "ACHR", "sma_100_drop", "reads sma_100, which is not a column"
            )

    assert len([r for r in caplog.records if "INERT" in r.message]) == 1
