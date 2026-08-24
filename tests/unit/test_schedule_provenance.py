"""A scheduled cycle must carry WHY it was scheduled.

`research_governor` records the catalyst on the schedule row (`reason_codes`,
`review_intent`, `urgency`) and then the row is a spent timer: a one-shot is
deactivated the moment it fires. The dispatch payload carried none of it, so
the cycle it started could not say what it was chasing, and nothing joined the
resulting `analysis_results` back to the catalyst that prompted them.

These pin the seam. The failure mode is silent — a missing key does not raise,
it just makes provenance permanently absent from every future cycle.
"""

import re
import pathlib

import pytest  # noqa: F401

from app.services.pipeline_service import (
    _trigger_source,
    _trigger_payload,
    _trigger_detail,
)

# The PLTR schedule exactly as it sits in production.
PLTR = {
    "dynamic_selection_mode": True,
    "schedule_id": "sch-bot-ba2c9d47",
    "research_reason": "Research: PLTR (PLTR Q2 FY2026 earnings after the bell)",
    "reason_codes": ["earnings_2026-08-03", "growth_deceleration"],
    "review_intent": "event_followup",
    "urgency": "high",
}


def test_scheduled_cycle_carries_its_catalyst():
    p = _trigger_payload(PLTR, ["PLTR"])
    assert p["schedule_id"] == "sch-bot-ba2c9d47"
    assert p["reason_codes"] == ["earnings_2026-08-03", "growth_deceleration"]
    assert p["review_intent"] == "event_followup"
    assert p["urgency"] == "high"


def test_a_scheduled_run_is_not_relabelled_as_the_research_governor():
    """`_trigger_source` checks `research_request` BEFORE
    `dynamic_selection_mode`. Setting it on the scheduler's payload would
    erase the distinction between a cycle that fired on a timer and one
    queued for immediate research — so the scheduler must not set it."""
    assert _trigger_source(PLTR) == "schedule"
    assert "research_request" not in PLTR


def test_a_watch_desk_wake_still_outranks_the_schedule_label():
    """Ordering regression: the Watch Desk sets dynamic_selection_mode too.
    A wake mislabelled as a schedule hides the tripwire that actually fired."""
    kwargs = dict(PLTR, watch_wake=True,
                  watch_trigger={"type": "price_below", "detail": "hit 21.40"})
    assert _trigger_source(kwargs) == "watch_desk"
    p = _trigger_payload(kwargs, ["PLTR"])
    assert p["trigger_type"] == "price_below"


def test_a_manual_run_grows_no_phantom_provenance():
    p = _trigger_payload({}, ["AAPL"])
    assert p["source"] == "manual"
    assert p["schedule_id"] is None
    assert p["reason_codes"] == []
    assert p["review_intent"] is None and p["urgency"] is None


def test_reason_codes_is_always_a_list_even_when_a_string_arrives():
    """`reason_codes` is STORED as a JSON string. cycle_scheduler decodes it
    before dispatch, but this must not emit a bare string if one slips
    through — downstream readers index it."""
    p = _trigger_payload({"reason_codes": '["a","b"]'}, [])
    assert isinstance(p["reason_codes"], list)
    p2 = _trigger_payload({"reason_codes": None}, [])
    assert p2["reason_codes"] == []


def test_the_human_readable_line_names_the_catalyst():
    detail = _trigger_detail(PLTR, ["PLTR"])
    assert "Scheduled cycle" in detail
    assert "PLTR" in detail
    assert "earnings_2026-08-03" in detail
    assert len(detail) <= 500


def test_provenance_keys_are_declared_known_or_they_are_dropped():
    """pipeline_service warns-and-ignores unknown START_CYCLE keys. An
    undeclared key does not raise — it vanishes into a log line."""
    src = pathlib.Path("app/services/pipeline_service.py").read_text()
    known = re.search(r"_known_keys = \{(.*?)\n        \}", src, re.S).group(1)
    for key in ("schedule_id", "reason_codes", "review_intent", "urgency"):
        assert f'"{key}"' in known, f"{key} would be dropped as an unknown payload key"


def test_the_scheduler_dispatch_payload_actually_sets_them():
    """The payload is built inline inside _execute_schedule, which needs a
    live APScheduler trigger to reach. Pin the source instead of the call."""
    src = pathlib.Path("app/services/cycle_scheduler.py").read_text()
    block = re.search(r"payload = \{(.*?)\n        \}", src, re.S).group(1)
    for key in ("schedule_id", "research_reason", "reason_codes",
                "review_intent", "urgency"):
        assert f'"{key}"' in block, f"dispatch payload does not carry {key}"
    assert '"research_request"' not in block, (
        "setting research_request would relabel every scheduled run"
    )


def test_the_schedule_column_list_is_defined_exactly_once():
    """It used to be copy-pasted at three call sites, each zipped positionally
    against its own hand-kept `cols` list — the shape where adding a field to
    three of four copies misaligns every column after it."""
    from app.services.cycle_scheduler import _SCHEDULE_COLS

    src = pathlib.Path("app/services/cycle_scheduler.py").read_text()
    assert src.count("'id', 'name', 'schedule_type', 'cron_expression'") == 0, (
        "an inline projection survived the extraction"
    )
    # Every provenance field the dispatch payload reads must be selected.
    for col in ("reason_codes", "review_intent", "urgency"):
        assert col in _SCHEDULE_COLS, f"{col} is never SELECTed, so s.get() is always None"
    assert len(_SCHEDULE_COLS) == len(set(_SCHEDULE_COLS)), "duplicate column"


def test_summary_provenance_is_promoted_out_of_the_json_blob():
    """A nested key is not a join key. These must be top-level columns on
    cycle_run_summaries or 'which cycles did this catalyst produce?' means
    unwinding a blob on every row."""
    src = pathlib.Path("app/log_manager.py").read_text()
    setblock = re.search(r"'\$set': \{(.*?)\n                    \}", src, re.S).group(1)
    for key in ("trigger_source", "schedule_id", "trigger_reason_codes",
                "review_intent", "urgency"):
        assert f"'{key}'" in setblock, f"{key} stays buried in summary_json"
