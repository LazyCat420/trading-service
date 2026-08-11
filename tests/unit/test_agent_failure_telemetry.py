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

def test_persisted_insert_binds_every_column_it_names(mock_db):
    """Column list and parameter list must stay the same length.

    `_persist_entries` builds its INSERT by hand; adding a column to one list
    and not the other is a runtime ProgrammingError that no import-time check
    would catch.
    """
    from app.v3 import telemetry as tel

    desk = _desk()
    _record_telemetry(
        desk, "v3_junior_analyst", 181747, 2, 15179, "AGENT_ERROR",
        attempt_no=1, failure_reason=SCHEMA_INVALID, error_message="boom",
    )
    tel._TABLE_ENSURED = True  # skip the DDL; we are checking the INSERT
    tel._persist_entries(desk, desk.agent_telemetry)

    inserts = [
        c for c in mock_db.execute.call_args_list
        if "INSERT INTO v3_agent_telemetry" in str(c.args[0])
    ]
    assert inserts, "no INSERT was issued"
    sql, params = inserts[0].args[0], inserts[0].args[1]

    columns = sql.split("(", 1)[1].split(")", 1)[0]
    n_columns = len([c for c in columns.split(",") if c.strip()])
    n_placeholders = sql.count("%s")
    assert n_columns == n_placeholders == len(params), (
        f"{n_columns} columns, {n_placeholders} placeholders, {len(params)} params"
    )

    for field in ("error_message", "failure_reason", "attempt_no"):
        assert field in sql, f"{field} is not persisted"
    assert SCHEMA_INVALID in params
    assert 1 in params
