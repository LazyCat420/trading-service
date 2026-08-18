"""The shared V3 prompt must name what the tool payload cannot hide.

WHY A PROMPT TEST. `enabled_tools` is not a restriction: prism's
`registerCustom()` drops `coreToolsLocked`, so `AgenticToolResolver` defaults a
CUSTOM_* persona to LOCKED and force-adds the whole CORE_AGENTIC set on top of
whatever we register. Filtering the list we send therefore cannot stop a model
from SEEING `execute_javascript` — only the DENY policy stops the call, and it
stops it *after* the model has spent a turn on it.

In cycle-v3-1786455000 that cost 14 turns (12 execute_javascript, 2
execute_command, every one POLICY_DENIED) and 4 of 20 emit_structured_output
calls were rejected for missing the `{"data": ...}` wrapper. The prompt is the
only surface we own, so these tests pin that it keeps saying so.
"""
import re

from app.v3.prism_registration import (
    _V3_COMMON_GUIDELINES,
    _V3_DENIED_TOOLS,
    _V3_TOOL_POLICIES,
)


def test_every_denied_tool_is_named_in_the_guidelines():
    """Tied to the real constant, so a new denial cannot skip the prompt."""
    missing = [t for t in _V3_DENIED_TOOLS if t not in _V3_COMMON_GUIDELINES]
    assert not missing, (
        f"denied but never mentioned to the model: {missing}. The DENY policy "
        f"blocks the call; only the prompt stops the model from spending a turn "
        f"discovering that."
    )


def test_the_guidelines_explain_the_structured_output_envelope():
    """The exact wrapper whose absence failed 4 of 20 calls in one cycle."""
    assert "emit_structured_output" in _V3_COMMON_GUIDELINES
    assert re.search(r'"data"\s*:', _V3_COMMON_GUIDELINES), (
        'the guidelines must show the literal {"data": {...}} envelope'
    )


def test_the_guidelines_forbid_data_as_a_string():
    """The half of the defect rule 8 originally missed.

    Re-measured over the 7 days to 2026-08-11: "'data' is required and must be
    an object" was 132 of 246 tool failures (54%) across 9 agents. Only 14 of
    the failing calls carried an empty `args_hash` — the "fields at the top
    level" shape the rule already described. The rest arrived with real
    arguments whose `data` was a JSON *string*.

    Both shapes are rejected with the SAME message, so the model cannot tell
    them apart from the error and re-sends the identical call until
    base_agent's 3-strike check aborts the agent. The prompt must therefore
    name the string case explicitly — the error never will.
    """
    assert re.search(
        r"not a string", _V3_COMMON_GUIDELINES, re.I
    ), (
        "rule 8 must say `data` cannot be a STRING. The executor's guard is "
        "`!data || typeof data !== 'object'`, so a stringified payload is "
        "rejected as if it were missing; only the prompt can explain that."
    )


def test_the_structured_output_examples_are_literal_json():
    """A mangled example teaches the wrong shape.

    The rule shows RIGHT/WRONG payloads. Because it lives inside a Python
    string, an escaped example can render as `\\"` — a double backslash the
    model would copy. Pin that the RIGHT example is parseable JSON exactly as
    the model receives it.
    """
    import json

    right = re.search(
        r"RIGHT:\s*(\{.*?\})\s*\n", _V3_COMMON_GUIDELINES
    )
    assert right, "rule 8 must carry a RIGHT: example"
    parsed = json.loads(right.group(1))
    assert isinstance(parsed.get("data"), dict), (
        "the RIGHT example must show `data` as a JSON object"
    )
    assert "\\\\" not in right.group(1), (
        "the example renders a double backslash — it will teach bad escaping"
    )


def test_execute_python_is_offered_as_the_permitted_alternative():
    """A denial with no substitute just leaves the model stuck.

    `execute_python` is a deliberate, reviewed exception (2026-08-03): it runs
    in tools-service as a sandboxed subprocess, not in this container.
    """
    assert "execute_python" in _V3_COMMON_GUIDELINES
    assert "execute_python" not in _V3_DENIED_TOOLS


def test_runtime_deny_policies_still_cover_every_denied_tool():
    """Defence in depth: the prompt persuades, the policy enforces.

    The prompt must never become the only thing standing between an agent and
    `execute_command`.
    """
    denied_by_policy = {
        p["tool"] for p in _V3_TOOL_POLICIES if p["decision"] == "DENY"
    }
    assert denied_by_policy == set(_V3_DENIED_TOOLS)


def test_guidelines_keep_the_original_pipeline_rules():
    """The additions must not have displaced the artifact contract."""
    for fragment in ("valid JSON artifact", "DataGap", "markdown code blocks"):
        assert fragment in _V3_COMMON_GUIDELINES
