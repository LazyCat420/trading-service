"""Regression tests for save_trading_chart overlay normalization.

Bug history (2026-07-14): agents saved overlays with string prices
("y0": "81"), alias types (horizontal_line, support_zone, sma_200), bare
`price` fields, and nested `price_range` objects. The frontend AgenticChart
component crashed on `ov.y0?.toFixed(2)` (string) — the "Component Error"
boundary in the Ticker Detail panel — and unknown types never rendered.
All shapes below were observed in production /app/data/charts/*.json files.
"""
from app.tools.charting_tools import normalize_overlays


def test_string_prices_coerced_to_float():
    out = normalize_overlays([{"type": "support", "y0": "81", "y1": "82.5"}])
    assert out == [{"type": "support", "y0": 81.0, "y1": 82.5, "reasoning": ""}]


def test_horizontal_line_with_price_and_label():
    out = normalize_overlays([
        {"color": "#ff0000", "label": "Resistance", "price": "20.87", "type": "horizontal_line"},
        {"color": "#00ff00", "label": "Support", "price": "19.2", "type": "horizontal_line"},
    ])
    assert out[0]["type"] == "resistance"
    assert out[0]["y0"] == out[0]["y1"] == 20.87
    assert out[0]["reasoning"] == "Resistance"
    assert out[1]["type"] == "support"


def test_nested_price_range_zones():
    out = normalize_overlays([
        {"price_range": {"y0": "330", "y1": "337"}, "type": "support_zone"},
        {"price_range": {"y0": "370", "y1": "377"}, "type": "resistance_zone"},
    ])
    assert out[0]["type"] == "support" and out[0]["y0"] == 330.0 and out[0]["y1"] == 337.0
    assert out[1]["type"] == "resistance"


def test_sma_alias_becomes_zone():
    out = normalize_overlays([{"type": "sma_200", "y0": "272.6", "y1": "272.6"}])
    assert out[0]["type"] == "zone"
    assert out[0]["y0"] == 272.6


def test_canonical_trendline_passthrough():
    out = normalize_overlays([{
        "type": "trendline", "x0": "2026-01-01", "x1": "2026-06-01",
        "y0": 100, "y1": 120, "reasoning": "ascending channel", "color": "green",
    }])
    assert out[0]["type"] == "trendline"
    assert out[0]["x0"] == "2026-01-01" and out[0]["x1"] == "2026-06-01"
    assert out[0]["y0"] == 100.0 and out[0]["y1"] == 120.0
    assert out[0]["color"] == "green"


def test_unusable_overlays_dropped():
    out = normalize_overlays([
        {"garbage": True},
        {"type": "support"},          # no prices at all
        {"type": "zone", "y0": "not-a-number", "y1": None},
        "not-a-dict",
        None,
    ])
    assert out == []


def test_one_sided_price_mirrored():
    out = normalize_overlays([{"type": "resistance", "y1": "89"}])
    assert out[0]["y0"] == out[0]["y1"] == 89.0


def test_none_input():
    assert normalize_overlays(None) == []
