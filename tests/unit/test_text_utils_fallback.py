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
