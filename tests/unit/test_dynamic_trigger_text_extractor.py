import pytest
from app.v3.orchestrator import extract_dynamic_trigger_from_text


def test_extract_dynamic_trigger_standard_prose():
    text = "Dynamic trigger sma_50_drop at $209.28 set as watch level for thesis reassessment."
    res = extract_dynamic_trigger_from_text(text)
    assert res == {"type": "sma_50_drop", "value": 209.28}


def test_extract_dynamic_trigger_colon_at_syntax():
    text = "Dynamic trigger: sma_20_drop @ $219.29 watch level."
    res = extract_dynamic_trigger_from_text(text)
    assert res == {"type": "sma_20_drop", "value": 219.29}


def test_extract_dynamic_trigger_rsi():
    text = "Setting dynamic trigger rsi_14_oversold at 30.0 for oversold bounce entry."
    res = extract_dynamic_trigger_from_text(text)
    assert res == {"type": "rsi_14_oversold", "value": 30.0}


def test_extract_dynamic_trigger_unevaluable_rejected():
    text = "Dynamic trigger imaginary_metric_break at $100."
    res = extract_dynamic_trigger_from_text(text)
    assert res is None


def test_extract_dynamic_trigger_no_mention():
    text = "Thesis is solid, holding until next earnings release."
    res = extract_dynamic_trigger_from_text(text)
    assert res is None


def test_extract_dynamic_trigger_rsi_oversold_variant():
    text = "Board conditional HOLD: dynamic trigger rsi_14_oversold @ 35.55 set."
    res = extract_dynamic_trigger_from_text(text)
    assert res == {"type": "rsi_14_oversold", "value": 35.55}
