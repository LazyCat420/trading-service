"""
Watch Desk auto-baseline extraction tests.

Regression guard for a silent bug the end-to-end cycle surfaced: the V3 verdict
nests the exit levels under `estimate` (estimate.stop_loss / estimate.take_profit),
but derive_baseline_watch used to read result["stop_loss"] / result["target"], so
the price invalidation + target triggers NEVER armed — only news + staleness did.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services import watch_desk


def _capture_triggers(result, snapshot=None):
    """Run derive_baseline_watch with create_watch mocked; return the trigger list."""
    with patch.object(watch_desk, "create_watch") as mock_create:
        watch_desk.derive_baseline_watch("NVDA", result, snapshot, "cycle-test")
        assert mock_create.called, "derive_baseline_watch should always arm a watch"
        return mock_create.call_args.kwargs["triggers"]


def _types(triggers):
    return {t["type"] for t in triggers}


def _level(triggers, ttype):
    return next(t["level"] for t in triggers if t["type"] == ttype)


def test_estimate_nested_levels_arm_price_triggers():
    # Real V3 shape: levels live under `estimate`, not top-level.
    result = {
        "action": "BUY",
        "estimate": {"stop_loss": 189.0, "take_profit": 300.0, "position_size_pct": 3.0},
        "rationale": "buy thesis",
    }
    triggers = _capture_triggers(result, snapshot={"price": 210.0})
    assert "price_below" in _types(triggers)
    assert "price_above" in _types(triggers)
    assert _level(triggers, "price_below") == 189.0
    assert _level(triggers, "price_above") == 300.0
    # News + staleness backstops always present; pct_change band from snapshot price.
    assert {"news", "staleness", "pct_change"} <= _types(triggers)


def test_legacy_top_level_levels_still_work():
    result = {"action": "BUY", "stop_loss": 100.0, "target": 150.0}
    triggers = _capture_triggers(result, snapshot={"price": 120.0})
    assert _level(triggers, "price_below") == 100.0
    assert _level(triggers, "price_above") == 150.0


def test_take_profit_top_level_maps_to_target():
    result = {"action": "BUY", "stop_loss": 100.0, "take_profit": 150.0}
    triggers = _capture_triggers(result, snapshot={"price": 120.0})
    assert _level(triggers, "price_above") == 150.0


def test_sell_drops_price_levels_keeps_news_staleness():
    # After a SELL the position is exited → stop/target are noise.
    result = {"action": "SELL", "estimate": {"stop_loss": 189.0, "take_profit": 300.0}}
    triggers = _capture_triggers(result, snapshot={"price": 210.0})
    assert "price_below" not in _types(triggers)
    assert "price_above" not in _types(triggers)
    assert {"news", "staleness"} <= _types(triggers)


def test_missing_snapshot_still_arms_estimate_levels():
    # Manual single-ticker cycles have no screener snapshot → price is None, but the
    # estimate levels must still arm (pct_change band is the only thing that drops).
    result = {"action": "BUY", "estimate": {"stop_loss": 189.0, "take_profit": 300.0}}
    triggers = _capture_triggers(result, snapshot=None)
    assert _level(triggers, "price_below") == 189.0
    assert _level(triggers, "price_above") == 300.0
    assert "pct_change" not in _types(triggers)
