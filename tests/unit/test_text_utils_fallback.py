import pytest
from app.utils.text_utils import parse_malformed_text_response

def test_parse_malformed_key_values():
    text = (
        "Here is my report for BCE:\n"
        "**action**: HOLD\n"
        "**confidence**: 85\n"
        "**conviction**: HIGH\n"
    )
    result = parse_malformed_text_response(text)
    assert result.get("action") == "HOLD"
    assert result.get("confidence") == 85
    assert result.get("conviction") == "HIGH"

def test_parse_malformed_lists():
    text = (
        "Analysis of UBS:\n"
        "**Core Claims**:\n"
        "- Underpriced valuation compared to peers\n"
        "- Strong macro headwinds are subsiding\n\n"
        "**Weaknesses**:\n"
        "- High dependency on interest rates\n"
        "- Regulatory compliance costs\n"
    )
    result = parse_malformed_text_response(text)
    assert result.get("core_claims") == [
        "Underpriced valuation compared to peers",
        "Strong macro headwinds are subsiding"
    ]
    assert result.get("weaknesses") == [
        "High dependency on interest rates",
        "Regulatory compliance costs"
    ]

def test_parse_malformed_json_like():
    text = (
        "Resulting JSON output was:\n"
        '{\n'
        '  "action": "BUY",\n'
        '  "confidence": 95\n'
        '}\n'
    )
    result = parse_malformed_text_response(text)
    assert result.get("action") == "BUY"
    assert result.get("confidence") == 95

def test_coercion_helpers():
    from app.utils.text_utils import coerce_str, coerce_int, coerce_list_str
    
    # Test coerce_str
    assert coerce_str("hello") == "hello"
    assert coerce_str(None) == ""
    assert coerce_str(123) == "123"
    assert coerce_str({"thesis_summary": "Summary here"}) == "Summary here"
    assert coerce_str({"other_key": "val"}) == '{"other_key": "val"}'
    assert coerce_str(["a", "b"]) == "a; b"
    
    # Test coerce_int
    assert coerce_int(75) == 75
    assert coerce_int("75") == 75
    assert coerce_int("75%") == 75
    assert coerce_int(0.75) == 75
    assert coerce_int("0.75") == 75
    assert coerce_int(1.0) == 100
    assert coerce_int("1.0") == 100
    assert coerce_int(0.0) == 0
    assert coerce_int("abc") == 0
    assert coerce_int(None) == 0
    
    # Test coerce_list_str
    assert coerce_list_str(["a", "b"]) == ["a", "b"]
    assert coerce_list_str("a\nb") == ["a", "b"]
    assert coerce_list_str("- item 1\n* item 2") == ["item 1", "item 2"]
    assert coerce_list_str({"key1": "val1"}) == ["key1: val1"]
    assert coerce_list_str(None) == []

def test_parse_malformed_overlays():
    from app.agents.technical_analyst_agent import parse_malformed_overlays
    text = (
        "Looking at the BCE OHLCV data, I need to assess support and resistance levels:\n"
        "- Major support is located at 145.50\n"
        "- Secondary support zone exists between 140.00 and 142.25\n"
        "- Resistance is strong near 155.00\n"
        "- Heavy selling resistance between 158.0 and 160\n"
    )
    result = parse_malformed_overlays(text)
    assert "overlays" in result
    overlays = result["overlays"]
    
    supports = [o for o in overlays if o["type"] == "support"]
    resistances = [o for o in overlays if o["type"] == "resistance"]
    
    assert len(supports) == 2
    assert len(resistances) == 2
    
    assert any(o["y0"] == 140.00 and o["y1"] == 142.25 for o in supports)
    assert any(o["y0"] == round(145.50 * 0.99, 2) and o["y1"] == round(145.50 * 1.01, 2) for o in supports)
    assert any(o["y0"] == round(155.00 * 0.99, 2) and o["y1"] == round(155.00 * 1.01, 2) for o in resistances)
    assert any(o["y0"] == 158.0 and o["y1"] == 160.0 for o in resistances)


def test_parse_json_list_response():
    from app.utils.text_utils import parse_json_list_response

    # Test case 1: Raw JSON array
    raw = '[{"ticker": "AAPL", "reason": "growth"}]'
    assert parse_json_list_response(raw) == [{"ticker": "AAPL", "reason": "growth"}]

    # Test case 2: Markdown JSON block
    raw_markdown = (
        "Here is the list:\n"
        "```json\n"
        '[\n'
        '  {"ticker": "MSFT", "reason": "AI"}\n'
        ']\n'
        "```\n"
        "Hope this helps."
    )
    assert parse_json_list_response(raw_markdown) == [{"ticker": "MSFT", "reason": "AI"}]

    # Test case 3: Nested JSON array with prefix/suffix prose and think block
    raw_think = (
        "<think>\n"
        "Let's think about this.\n"
        "</think>\n"
        "JSON output:\n"
        '[{"ticker": "TSLA", "reason": "EV"}]'
    )
    assert parse_json_list_response(raw_think) == [{"ticker": "TSLA", "reason": "EV"}]

    # Test case 4: Empty / malformed response
    assert parse_json_list_response("") == []
    assert parse_json_list_response("No tickers found.") == []


