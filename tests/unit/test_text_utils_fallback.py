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
    assert coerce_int("abc") == 0
    assert coerce_int(None) == 0
    
    # Test coerce_list_str
    assert coerce_list_str(["a", "b"]) == ["a", "b"]
    assert coerce_list_str("a\nb") == ["a", "b"]
    assert coerce_list_str("- item 1\n* item 2") == ["item 1", "item 2"]
    assert coerce_list_str({"key1": "val1"}) == ["key1: val1"]
    assert coerce_list_str(None) == []
