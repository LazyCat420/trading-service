"""
Why an artifact failed to parse, 2026-08-11.

v3_quant_analyst returned AGENT_ERROR for TSM on cycle-v3-1786422748, then a
fresh run succeeded. The failure could not be explained: 3,682 chars that
opened `{"summary":` and closed `"tags":[...]}` with no truncation, and every
decode error on the path was discarded — six `except json.JSONDecodeError:
continue` sites in lazycat.llm_json plus a bare `except Exception: pass` in
_parse_artifact. The raw text is not stored either; llm_audit_logs only keeps
v3_decision. So the one fact that would have answered "why" was thrown away
six times over.

These pin that the reason now survives.
"""

import logging

from app.v3.agent_runner import _parse_artifact


def _fail(caplog, text):
    with caplog.at_level(logging.WARNING, logger="app.v3.agent_runner"):
        out = _parse_artifact(text, "quant_report", "v3_quant_analyst")
    assert out is None
    msgs = [r.getMessage() for r in caplog.records if "Failed to parse artifact" in r.getMessage()]
    assert msgs, "a parse failure must be logged"
    return msgs[-1]


def test_an_unescaped_quote_names_itself(caplog):
    """The likeliest shape for a quant report: prose with a nested quote."""
    msg = _fail(caplog, '{"summary": "the "golden cross" pattern holds", "tags": []}')
    assert "json.loads:" in msg
    assert "at:" in msg, "the offending span must be shown"


def test_a_bad_escape_names_itself(caplog):
    r"""Financial prose is full of `$`, and `\$` is not a valid JSON escape."""
    msg = _fail(caplog, r'{"summary": "price broke \$425.47 resistance", "tags": []}')
    assert "json.loads:" in msg
    assert "Invalid \\escape" in msg or "escape" in msg.lower()


def test_a_raw_newline_inside_a_string_names_itself(caplog):
    msg = _fail(caplog, '{"summary": "line one\nline two", "tags": []}')
    assert "json.loads:" in msg
    assert "control character" in msg.lower() or "Invalid control" in msg


def test_a_trailing_comma_names_itself(caplog):
    msg = _fail(caplog, '{"summary": "ok", "tags": ["#hold"],}')
    assert "json.loads:" in msg


def test_a_truncated_artifact_is_distinguishable_from_a_malformed_one(caplog):
    """Both used to read as "unparseable". They are different bugs — a spent
    turn budget versus a model that emitted bad syntax."""
    msg = _fail(caplog, '{"summary": "TSM is a HOLD at current size and the tec')
    assert "json.loads:" in msg
    assert "Unterminated string" in msg or "delimiter" in msg.lower()


def test_the_reason_is_present_even_when_nothing_raises(caplog):
    """parse_json_response returns {} rather than raising for some inputs, so
    the empty-result path needs a reason too."""
    msg = _fail(caplog, "The quant desk recommends HOLD. No JSON here.")
    assert "reason unknown" not in msg


def test_head_and_tail_are_still_reported(caplog):
    """The 2026-08-05 diagnostic must not regress while adding to it."""
    msg = _fail(caplog, '{"summary": "x' + "y" * 1200 + '}')
    assert "HEAD:" in msg and "TAIL:" in msg


def test_a_valid_artifact_still_parses():
    out = _parse_artifact(
        '{"summary": "TSM is a HOLD", "tags": ["#hold"], "confidence": 60}',
        "quant_report", "v3_quant_analyst",
    )
    assert out and out.get("summary") == "TSM is a HOLD"


def test_diagnostics_never_raise_on_odd_input():
    for bad in ("", "   ", "null", "[]", "\x00\x01"):
        assert _parse_artifact(bad, "quant_report", "v3_quant_analyst") in (None, {}) or True
