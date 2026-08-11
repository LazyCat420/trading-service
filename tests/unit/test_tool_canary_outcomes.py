"""
ToolCanary outcome semantics (2026-08-10 cycle audit).

The canary's own comment says a FORBIDDEN line "means the DENY did not apply".
It did not mean that: it fired on the ATTEMPT and never read the result, so
cycle-v3-1786401874 logged 14 ERROR-level policy failures while the DENY
policy held on all 14. These tests pin both directions — the alarm must stay
silent when the guard works and must still fire when it does not.
"""

import logging

import pytest

from app.v3.tool_telemetry import _canary_check, _forbidden_call_executed


@pytest.fixture
def violations(monkeypatch):
    """Capture record_violation calls without touching the database."""
    seen = []

    def _fake(kind, **kwargs):
        seen.append((kind, kwargs))
        return kind

    import app.v3.invariants as invariants
    monkeypatch.setattr(invariants, "record_violation", _fake)
    return seen


# ── The distinction the canary could not previously make ───────────────

def test_policy_denied_result_means_the_call_never_ran():
    assert _forbidden_call_executed("POLICY_DENIED", False) is False


def test_tool_loop_detector_block_means_the_call_never_ran():
    assert _forbidden_call_executed("", True) is False


@pytest.mark.parametrize("error_message", ["", "Timeout", "some other failure"])
def test_anything_unrecognised_counts_as_executed(error_message):
    """Fail loud. A denial marker nobody has seen yet must not mute the alarm."""
    assert _forbidden_call_executed(error_message, False) is True


# ── End-to-end through the canary ──────────────────────────────────────

def test_denied_forbidden_call_logs_info_and_records_no_violation(caplog, violations):
    with caplog.at_level(logging.INFO, logger="app.v3.tool_telemetry"):
        _canary_check(
            "v3_bull_defense", "execute_javascript",
            error_message="POLICY_DENIED", was_blocked=False,
            cycle_id="cycle-v3-1786401874", ticker="META",
        )

    assert violations == [], "a held DENY must not record a violation"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("FORBIDDEN TOOL ATTEMPTED" in r.message for r in caplog.records)
    assert any("HELD" in r.message for r in caplog.records)


def test_executed_forbidden_call_still_errors_and_records_a_violation(caplog, violations):
    with caplog.at_level(logging.INFO, logger="app.v3.tool_telemetry"):
        _canary_check(
            "v3_junior_analyst", "execute_command",
            error_message="", was_blocked=False,
            cycle_id="cycle-v3-1786401874", ticker="XOM",
        )

    assert [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "a forbidden tool that RAN is a security regression and must stay ERROR"
    assert len(violations) == 1
    kind, kwargs = violations[0]
    assert kind == "FORBIDDEN_TOOL_EXECUTED"


def test_violation_carries_the_cycle_and_ticker(violations):
    """They were plain kwargs on record_violation and were simply never passed,
    so every row landed NULL and every log line read "for ? (cycle=?)"."""
    _canary_check(
        "v3_bull_agent", "execute_javascript",
        error_message="", was_blocked=False,
        cycle_id="cycle-v3-1786401874", ticker="SMCI",
    )
    _, kwargs = violations[0]
    assert kwargs["cycle_id"] == "cycle-v3-1786401874"
    assert kwargs["ticker"] == "SMCI"
    assert kwargs["agent"] == "v3_bull_agent"
    assert kwargs["tool"] == "execute_javascript"


def test_off_whitelist_but_not_forbidden_is_unchanged(caplog, violations):
    """execute_python was deliberately removed from _FORBIDDEN on 2026-08-03."""
    with caplog.at_level(logging.INFO, logger="app.v3.tool_telemetry"):
        _canary_check("v3_quant_analyst", "execute_python", error_message="")
    assert violations == []
    assert any("OFF-WHITELIST" in r.message for r in caplog.records)


def test_a_whitelisted_tool_says_nothing(caplog, violations):
    with caplog.at_level(logging.INFO, logger="app.v3.tool_telemetry"):
        _canary_check("v3_bull_defense", "whiteboard_read", error_message="")
    assert violations == []
    assert not [r for r in caplog.records if "ToolCanary" in r.message]
