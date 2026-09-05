"""The entry conditions the desk stated and the monitor threw away.

Measured 2026-08-20 over 251 dynamic triggers on HOLDs: 24% could never fire,
and they were disproportionately the ENTRY-side spellings — `sma_50_reclaim`
alone was 23 — on a book whose only executable action is BUY.

Every setup string below is a real live value from that census.
"""

import pytest

from app.trading.order_triggers import (
    dynamic_trigger_is_evaluable,
    normalize_dynamic_trigger_type,
)

#: The inert setups the census found, with their counts, and whether an
#: unambiguous rewrite exists. 33 of the 59 are recoverable — 56%.
RECOVERABLE = ["sma_50_reclaim", "sma_50_breakout", "sma_200_reclaim",
               "sma_20_reclaim"]
UNMAPPABLE = ["sma_200_break", "sma_50_break", "sma_50_cross",
              "breakout_above_resistance", "resistance_breakout",
              "resistance_break", "support_drop", "support_bounce"]
ALREADY_FINE = ["sma_50_drop", "sma_200_drop", "sma_20_drop", "sma_50_rise",
                "rsi_14_oversold", "rsi_14_overbought", "trailing_drop",
                "sma_200_cross_above"]


@pytest.mark.parametrize("setup", RECOVERABLE)
def test_a_known_synonym_is_rewritten_so_it_can_fire(setup):
    """`reclaim` and `breakout` both mean "price gets back above the average",
    which the checker spells `rise`."""
    assert not dynamic_trigger_is_evaluable(setup), \
        f"{setup} is supposed to be the broken case this fixes"
    out = normalize_dynamic_trigger_type(setup)
    assert out != setup
    assert dynamic_trigger_is_evaluable(out)
    # The METRIC must survive the rewrite — turning sma_200_reclaim into
    # sma_50_rise would watch the wrong average and look like it worked.
    assert out.split("_")[1] == setup.split("_")[1]
    assert out.endswith("_rise")


@pytest.mark.parametrize("setup", UNMAPPABLE)
def test_an_ambiguous_or_metricless_setup_is_left_alone(setup):
    """A bare `break` names no direction and `resistance_*` names no column
    the checker can read. Guessing here would arm a watch on a condition the
    desk never stated — worse than not arming one."""
    assert normalize_dynamic_trigger_type(setup) == setup
    assert not dynamic_trigger_is_evaluable(setup)


@pytest.mark.parametrize("setup", ALREADY_FINE)
def test_a_working_setup_is_never_touched(setup):
    assert dynamic_trigger_is_evaluable(setup)
    assert normalize_dynamic_trigger_type(setup) == setup


@pytest.mark.parametrize("junk", ["", "   ", None])
def test_empty_input(junk):
    assert normalize_dynamic_trigger_type(junk) == ""


def test_normalisation_never_returns_something_that_cannot_fire():
    """The guarantee the caller relies on: if the string CHANGED, the result
    is evaluable. Anything else would move the failure instead of fixing it."""
    for setup in RECOVERABLE + UNMAPPABLE + ALREADY_FINE:
        out = normalize_dynamic_trigger_type(setup)
        if out != setup:
            assert dynamic_trigger_is_evaluable(out), setup


#: THE REAL INERT CENSUS, transcribed whole from the live query on 2026-08-20
#: (`analysis_results`, HOLDs since 08-01, production cycles, setups the
#: evaluator rejects). Every entry is measured. An earlier draft of this test
#: invented two rows to make its total reach 59, which is the defect of
#: manufacturing a value to satisfy a check and then reading it as evidence —
#: the fabricated total agreed with itself and with nothing else.
_INERT_CENSUS_2026_08_20 = {
    "sma_50_reclaim": 23, "sma_50_breakout": 6, "sma_200_break": 3,
    "breakout_above_resistance": 3, "resistance_breakout": 3,
    "sma_200_reclaim": 2, "support_drop": 2, "sma_20_reclaim": 2,
    "sma_50_break": 2, "resistance_break": 2, "support_bounce": 1,
    "sma_50_cross": 1, "price_above": 1, "price_breakout": 1,
    "price_pullback_to_support": 1, "price_drop": 1, "support_breach": 1,
    "support_break": 1, "sma_200_breakout": 1, "sma_100_drop": 1,
    "close_above_sma_50": 1,
}


def test_the_recovered_share_is_what_was_claimed():
    """34 of the 59 inert triggers, by OCCURRENCE and not by distinct name.

    Pinned so that widening the synonym table later has to restate the number
    it claims rather than quietly changing what "58%" refers to.
    """
    total = sum(_INERT_CENSUS_2026_08_20.values())
    recovered = sum(
        n for s, n in _INERT_CENSUS_2026_08_20.items()
        if dynamic_trigger_is_evaluable(normalize_dynamic_trigger_type(s)))
    assert total == 59, "the census total moved; re-derive the claim"
    assert recovered == 34, recovered
    assert round(100 * recovered / total) == 58

    # The long tail is real and mostly UNMAPPABLE: `price_above` and
    # `close_above_sma_50` name no sma_/rsi_ metric column in the position the
    # parser reads, and `support_*` names no column at all. They stay refused.
    assert not dynamic_trigger_is_evaluable(
        normalize_dynamic_trigger_type("close_above_sma_50"))


@pytest.mark.asyncio
async def test_create_trigger_refuses_an_unevaluable_setup(monkeypatch):
    """It used to accept one and let `retire_inert_dynamic_triggers` delete it
    later — the desk's condition vanished with nothing telling the caller."""
    from app.trading import order_triggers

    wrote = []
    monkeypatch.setattr(order_triggers.mongo_store, "insert_docs",
                        lambda c, d, **kw: wrote.append((c, d)))
    monkeypatch.setattr(order_triggers.mongo_store, "update_docs",
                        lambda *a, **kw: None)

    out = await order_triggers.create_trigger(
        bot_id="b", ticker="AAPL", trigger_type="dynamic", trigger_price=0.0,
        dynamic_trigger_type="resistance_breakout", dynamic_trigger_value=1.0)
    assert "error" in out
    assert not wrote, "an unevaluable setup must not reach the store"


@pytest.mark.asyncio
async def test_create_trigger_stores_the_normalised_setup(monkeypatch):
    """The stored row must already be evaluable — normalising on READ would
    leave the sweeper free to delete the row before anything read it."""
    from app.trading import order_triggers

    wrote = []
    monkeypatch.setattr(order_triggers.mongo_store, "insert_docs",
                        lambda c, d, **kw: wrote.append((c, d)))
    monkeypatch.setattr(order_triggers.mongo_store, "update_docs",
                        lambda *a, **kw: None)
    # `create_trigger` now asks whether the condition is ALREADY true before
    # arming (2026-09-05), which reads the last close and the technicals
    # column, so this test has to say what the market looked like. `sma_50_rise`
    # fires when price CROSSES ABOVE the average, so a price of $140 under a
    # $145 SMA-50 is a watch that has not yet triggered — which is the state
    # this test is about. (Getting this backwards is what the new guard is for:
    # at $150 the same setup is already true and the row is stored inactive.)
    monkeypatch.setattr(order_triggers, "_get_current_price",
                        lambda t: (140.0, "fixture"))
    monkeypatch.setattr(order_triggers, "_current_metric",
                        lambda t, col: 145.0)

    out = await order_triggers.create_trigger(
        bot_id="b", ticker="AAPL", trigger_type="dynamic", trigger_price=0.0,
        dynamic_trigger_type="sma_50_reclaim", dynamic_trigger_value=145.0)
    assert "error" not in out, out
    assert wrote, "a normalisable setup must be stored"
    doc = wrote[0][1][0]
    assert doc["dynamic_trigger_type"] == "sma_50_rise"
    assert dynamic_trigger_is_evaluable(doc["dynamic_trigger_type"])
    assert doc["active"] is True, "price is below the level; the watch is real"
    assert doc["inert_reason"] is None
