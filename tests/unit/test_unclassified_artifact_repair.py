"""The UNCLASSIFIED class, against the buffers that actually produced it.

UNCLASSIFIED was 75% of every output-rule repair failure (15 of 20 over the 30
days to 2026-09-12; 88 firings, 82.1% repair success against 96.5% overall).
`classify_output` returns it when JSON is present and balanced and still will
not parse, which is a statement about the PARSER, so the fixtures here are the
real buffers and not paraphrases of them:

  * `tests/unit/fixtures/unclassified_artifact_buffers.json` — eight verbatim
    agent replies, pulled 2026-09-12 from `llm_audit_logs.raw_response` and
    from `pipeline_trace_blobs` joined through `pipeline_trace_events`
    (stage `model.output`). Every one of them is a buffer `parse_json_response`
    returned empty for in production.

Three of them are the defect this file fixes. Five of them are buffers that
MUST stay unparseable, and they are the non-vacuity control: a repair that
rescues a prompt template is not a parser fix, it is a fabricator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.utils.text_utils import parse_json_response
from app.v3.output_rules import classify_output

_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "unclassified_artifact_buffers.json").read_text()
)

#: The model dropped ONE comma and the whole artifact was thrown away.
REPAIRABLE = (
    "missing_comma__narrative_curator_BCE",
    "missing_comma__narrative_curator_AMP",
    "trailing_comma__v3_bear_agent",
)

#: Buffers whose JSON is broken in ways a comma cannot fix, or which were
#: never an artifact at all. Recovering any of these would be a regression.
MUST_STAY_UNPARSEABLE = (
    "prompt_echo__planner_recovery",
    "prompt_echo__curator_recovery",
    "unquoted_value__pillar_adjuster",
    "thousands_separator__technical_analyst",
    "pseudo_tool_call__technical_analyst",
)

#: What each repairable buffer must come back holding. Keys the MODEL wrote —
#: asserted so a future "repair" that invents a schema-clean artifact out of
#: prose fails here instead of shipping.
EXPECTED_KEYS = {
    "missing_comma__narrative_curator_BCE": {"story_summary", "key_themes"},
    "missing_comma__narrative_curator_AMP": {"story_summary", "key_themes"},
    "trailing_comma__v3_bear_agent": {
        "summary",
        "rebuttals",
        "independent_risks",
        "target_downside",
        "preferred_alternative",
        "confidence",
    },
}


def test_the_fixtures_are_the_real_thing():
    """Non-vacuity, part 1: these buffers are unparseable by plain json."""
    for name, text in _FIXTURES.items():
        with pytest.raises(ValueError):
            json.loads(text)
        assert len(text) > 400, name


@pytest.mark.parametrize("name", REPAIRABLE)
def test_a_dropped_comma_no_longer_costs_the_whole_artifact(name):
    """The exact defect: one missing or one stray comma, nothing else.

    RED before the fix — `parse_json_response` returned `{}` for all three.
    """
    text = _FIXTURES[name]

    recovered = parse_json_response(text)

    assert isinstance(recovered, dict) and recovered, (
        f"{name}: the model's object was discarded over a comma"
    )
    assert EXPECTED_KEYS[name] <= set(recovered), (
        f"{name}: expected the model's own keys, got {sorted(recovered)}"
    )


@pytest.mark.parametrize("name", REPAIRABLE)
def test_the_recovered_object_carries_only_what_the_model_wrote(name):
    """No field is manufactured: every recovered key appears in the buffer."""
    text = _FIXTURES[name]

    recovered = parse_json_response(text)

    for key in recovered:
        assert f'"{key}"' in text, f"{name}: key {key!r} is not in the buffer"


@pytest.mark.parametrize("name", MUST_STAY_UNPARSEABLE)
def test_a_buffer_that_is_not_an_artifact_is_still_refused(name):
    """Non-vacuity, part 2: the control.

    A prompt echo (`{"action": "BUY" or "SELL", ...}` — a TEMPLATE the model
    quoted back), an unquoted value, thousands separators inside a number and
    a pseudo tool call are all still `{}`. Each of these would produce a
    plausible-looking artifact if the repair were allowed to guess.
    """
    assert parse_json_response(_FIXTURES[name]) == {}


@pytest.mark.parametrize("name", REPAIRABLE)
def test_the_class_these_buffers_used_to_land_in(name):
    """They were UNCLASSIFIED, which is why they were worth fixing.

    `classify_output` is only consulted once parsing has failed, so this pins
    the BEFORE state as the reason this file exists. It reads the same buffer
    the classifier read: JSON present, balanced, and refused.
    """
    assert classify_output(_FIXTURES[name]).name == "UNCLASSIFIED"


def test_the_repair_is_lexical_and_idempotent():
    """A comma restored once is not restored twice."""
    from app.utils.text_utils import _repair_json_delimiters

    for name in REPAIRABLE:
        once = _repair_json_delimiters(_FIXTURES[name])
        assert _repair_json_delimiters(once) == once, name


def test_a_valid_artifact_is_never_touched():
    """The control the repair itself needs: valid JSON goes through unchanged."""
    from app.utils.text_utils import _repair_json_delimiters

    artifact = json.dumps(
        {"summary": "A sentence, with a comma, inside it.", "confidence": 68},
        indent=2,
    )
    assert _repair_json_delimiters(artifact) == artifact
    assert parse_json_response(artifact)["confidence"] == 68
