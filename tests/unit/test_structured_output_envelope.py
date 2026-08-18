"""The `emit_structured_output` envelope must not read as a collapsed artifact.

`emit_structured_output` is on no v3 whitelist, but Prism offers it to the
personas anyway — the ToolCanary logged an OFF-WHITELIST warning for the quant
and valuation analysts on 2026-08-03 — and the models use it. Its request shape
buries the artifact one level down under `data`, which parses as clean JSON, so
the unparseable-output salvage pass never fires; instead every required field
reads as missing and `_artifact_collapsed` calls the run a total collapse.

Measured on cycle-v3-1785792600 (PLTR): the junior analyst and the quant
analyst each returned AGENT_ERROR with ALL required fields reported missing,
and each was re-run from scratch — ~185s spent re-deriving research that had
already been produced correctly.

The envelope shapes below are the four observed in `agent_traces.tool_args`
over 2026-08-03 (7 failed calls, all that day).
"""
import json

import pytest

from app.v3.agent_runner import (
    _artifact_collapsed,
    _parse_artifact,
    _unwrap_structured_output,
)

# The artifact the junior analyst actually produced — intact, just wrapped.
DESK_NOTE = {
    "summary": "PLTR beat and raised; US commercial +149% YoY.",
    "key_findings": ["Q2 revenue $1.94B vs $1.81B consensus"],
    "data_gaps": [],
    "confidence": 70,
    "triage_recommendation": "monitor",
}

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}


def _parse(payload: str) -> dict | None:
    return _parse_artifact(payload, "desk_note", "v3_junior_analyst")


def test_plain_envelope_is_unwrapped():
    """`{schema, label, data}` — the tool's own request shape."""
    parsed = _parse(json.dumps({"schema": SCHEMA, "label": "desk_note", "data": DESK_NOTE}))

    assert parsed == DESK_NOTE
    assert not _artifact_collapsed("desk_note", parsed), (
        "an intact artifact must not read as a collapsed one"
    )


def test_stringified_data_is_unwrapped():
    """The model stringified its own payload.

    This is the same defect that makes the tool reject the call outright with
    "'data' is required and must be an object" (4 of the 7 observed failures).
    """
    parsed = _parse(json.dumps({"schema": SCHEMA, "data": json.dumps(DESK_NOTE)}))

    assert parsed == DESK_NOTE


def test_double_encoded_arguments_wrapper_is_unwrapped():
    """`{"arguments": "<json>"}` — verbatim shape from 2026-08-03 21:39:10Z.

    The provider adapter left the OpenAI-style function-call envelope encoded,
    so the artifact sits two levels down.
    """
    inner = json.dumps({"schema": SCHEMA, "label": "desk_note", "data": DESK_NOTE})
    parsed = _parse(json.dumps({"arguments": inner}))

    assert parsed == DESK_NOTE


def test_synthetic_output_response_shape_is_unwrapped():
    """The tool's *response* echoed back, not its request."""
    parsed = _parse(json.dumps({
        "acknowledged": True, "label": "desk_note",
        "data": DESK_NOTE, "_synthetic": True,
    }))

    assert parsed == DESK_NOTE


# ── Fail-closed: unwrapping must never touch a real artifact ────────────────

def test_a_real_artifact_with_its_own_data_field_is_untouched():
    """A `data` key alongside the artifact's own fields is NOT an envelope."""
    artifact = dict(DESK_NOTE, data={"anything": 1})
    parsed = _parse(json.dumps(artifact))

    assert parsed == artifact, "an artifact that kept its own fields must survive intact"


def test_ordinary_artifact_is_untouched():
    parsed = _parse(json.dumps(DESK_NOTE))
    assert parsed == DESK_NOTE


@pytest.mark.parametrize("payload", [
    {"schema": SCHEMA, "data": {}},          # empty payload — nothing to recover
    {"schema": SCHEMA, "data": "not json"},  # undecodable string
    {"schema": SCHEMA, "data": [1, 2, 3]},   # wrong type
    {"schema": SCHEMA, "label": "x"},        # no `data` at all
    {"arguments": "{not json"},              # undecodable wrapper
])
def test_unrecoverable_envelopes_are_left_alone(payload):
    """No `data` dict to recover means we return the input unchanged.

    The run still fails — correctly — rather than the unwrap inventing a shape.
    """
    assert _unwrap_structured_output(payload, "desk_note", "agent") == payload


def test_the_regression_this_fixes():
    """Positive control: this exact input returned AGENT_ERROR before the fix."""
    envelope = json.dumps({"schema": SCHEMA, "label": "desk_note", "data": DESK_NOTE})

    # Without the unwrap the parsed top level is envelope furniture only, and
    # `_artifact_collapsed` is what turns that into AGENT_ERROR.
    raw = json.loads(envelope)
    assert _artifact_collapsed("desk_note", raw), (
        "control: the raw envelope must be the shape that trips the collapse gate"
    )
    assert not _artifact_collapsed("desk_note", _parse(envelope))
