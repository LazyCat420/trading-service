"""A failed agent run must record WHY it failed and WHICH attempt it was.

Before 2026-08-11 `v3_agent_telemetry` recorded that a run produced
AGENT_ERROR and nothing else — the crash path passed the exception to the
logger and then dropped it, so an AGENT_ERROR row could equally be a timeout,
a schema rejection, or a model that answered with nothing. And a retried agent
wrote two rows 17µs apart (ASIC/v3_junior_analyst, cycle-v3-1786455000, quality
-1 then 88) with no way to tell which came first.
"""
import pytest

from app.v3.agent_runner import _record_telemetry
from app.v3.output_rules import FAILURE_REASONS, SCHEMA_INVALID
from app.v3.shared_desk import SharedDesk
from app.v3.telemetry import sanitize_error_message


def _desk() -> SharedDesk:
    return SharedDesk(cycle_id="cycle-v3-1786455000", ticker="ASIC")


def _only_entry(desk: SharedDesk) -> dict:
    assert len(desk.agent_telemetry) == 1
    return desk.agent_telemetry[0]


# ── sanitize_error_message ──────────────────────────────────────────────

def test_sanitizer_keeps_the_exception_not_the_word_traceback():
    """A traceback's LAST line names the failure; its first says 'Traceback'."""
    raw = (
        "Traceback (most recent call last):\n"
        '  File "agent_runner.py", line 1919, in run_v3_agent\n'
        "    artifact = _parse_artifact(text)\n"
        "ValueError: unterminated string starting at line 3\n"
    )
    out = sanitize_error_message(raw)
    assert out == "ValueError: unterminated string starting at line 3"
    assert not out.startswith("Traceback")


def test_sanitizer_flattens_and_caps():
    out = sanitize_error_message("a\r\nb\tc" + "X" * 1000, max_length=64)
    assert "\n" not in out and "\r" not in out and "\t" not in out
    assert len(out) <= 64
    assert out.endswith("…"), "a clipped message must show that it was clipped"


def test_sanitizer_collapses_control_characters():
    assert sanitize_error_message("a\x00\x07b   c") == "a b c"


def test_sanitizer_passes_through_a_normal_message():
    msg = "ValueError: ticker must not be empty"
    assert sanitize_error_message(msg) == msg


@pytest.mark.parametrize("empty", ["", None])
def test_sanitizer_handles_no_error(empty):
    assert sanitize_error_message(empty) == ""


# ── _record_telemetry wiring ────────────────────────────────────────────

def test_failure_row_carries_reason_message_and_attempt():
    desk = _desk()
    _record_telemetry(
        desk, "v3_junior_analyst", 181747, 2, 15179, "AGENT_ERROR",
        attempt_no=1,
        failure_reason=SCHEMA_INVALID,
        error_message="desk_note missing required fields: summary",
    )
    entry = _only_entry(desk)
    assert entry["failure_reason"] == SCHEMA_INVALID
    assert entry["error_message"] == "desk_note missing required fields: summary"
    assert entry["attempt_no"] == 1
    assert entry["outcome"] == "AGENT_ERROR"


def test_the_recorded_message_is_sanitized_not_raw():
    """The writer sanitizes; callers hand it raw exception text."""
    desk = _desk()
    _record_telemetry(
        desk, "v3_junior_analyst", 10, 0, 0, "AGENT_ERROR",
        failure_reason="RUNNER_EXCEPTION",
        error_message="RuntimeError: boom\nsecond line\nthird line",
    )
    msg = _only_entry(desk)["error_message"]
    assert "\n" not in msg
    assert msg == "RuntimeError: boom second line third line"


def test_success_row_records_the_attempt_but_no_failure_fields():
    """A SUCCESSFUL retry is the case the column exists for (ASIC row 6599)."""
    desk = _desk()
    _record_telemetry(
        desk, "v3_junior_analyst", 157620, 2, 14388, "SUCCESS", 88,
        attempt_no=2,
    )
    entry = _only_entry(desk)
    assert entry["attempt_no"] == 2
    assert entry["quality_score"] == 88
    assert not entry["error_message"]
    assert entry["failure_reason"] is None


def test_a_reason_outside_the_namespace_is_quarantined(caplog):
    """An invented class must not silently fork the taxonomy."""
    desk = _desk()
    with caplog.at_level("ERROR"):
        _record_telemetry(
            desk, "v3_bull_agent", 5, 0, 0, "AGENT_ERROR",
            failure_reason="POLICY_DENIED",  # a tool-level fact, not an agent class
        )
    entry = _only_entry(desk)
    assert entry["failure_reason"] == "UNCLASSIFIED"
    assert entry["failure_reason"] in FAILURE_REASONS
    assert "not in the output_rules namespace" in caplog.text


def test_defaults_keep_a_plain_run_clean():
    desk = _desk()
    _record_telemetry(desk, "v3_quant_analyst", 100, 1, 50, "SUCCESS", 90)
    entry = _only_entry(desk)
    assert entry["attempt_no"] == 1
    assert entry["failure_reason"] is None
    assert entry["error_message"] == ""


# ── the INSERT actually carries them ────────────────────────────────────

def test_persisted_insert_binds_every_column_it_names():
    """The persisted document must actually carry the failure fields.

    This used to build an INSERT by hand and count that the column list and
    the parameter list were the same length — a hand-written SQL hazard that
    the Mongo rewrite removed. `_persist_entries` writes a document through
    `mongo_store.insert_docs` now, so the equivalent (and stronger) check is
    that every field the diagnosis needs is present in the document with the
    recorded VALUE, not merely named in a string.
    """
    from unittest.mock import patch

    from app.v3 import telemetry as tel

    desk = _desk()
    _record_telemetry(
        desk, "v3_junior_analyst", 181747, 2, 15179, "AGENT_ERROR",
        attempt_no=1, failure_reason=SCHEMA_INVALID, error_message="boom",
    )
    tel._TABLE_ENSURED = True  # skip the DDL; we are checking the write
    with patch("app.v3.telemetry.mongo_store.insert_docs") as insert_docs:
        tel._persist_entries(desk, desk.agent_telemetry)

    assert insert_docs.call_count == 1, "no write was issued"
    collection, docs = insert_docs.call_args[0][:2]
    assert collection == "v3_agent_telemetry"
    assert len(docs) == 1
    doc = docs[0]

    for field in ("error_message", "failure_reason", "attempt_no"):
        assert field in doc, f"{field} is not persisted"
    assert doc["failure_reason"] == SCHEMA_INVALID
    assert doc["attempt_no"] == 1
    assert doc["error_message"] == "boom"
    # The identifying columns must ride along too, or the row cannot be
    # attributed to a cycle/ticker/agent.
    assert doc["cycle_id"] == desk.cycle_id
    assert doc["ticker"] == desk.ticker
    assert doc["agent_name"] == "v3_junior_analyst"
    assert doc["outcome"] == "AGENT_ERROR"
