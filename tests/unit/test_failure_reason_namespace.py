"""The failure-reason namespace has exactly one owner.

`v3_agent_telemetry.failure_reason` and `v3_guardrail_firings.guardrail`
(`output_rule:*`) describe the same events in the same words on purpose: a
telemetry row is meant to join its rule firing. These tests pin that contract,
because the way it breaks is silent — a second enum defined next to a writer,
agreeing with `classify_output` on the easy cases and drifting on the rest.
"""
import inspect

import pytest

from app.v3 import output_rules
from app.v3.output_rules import (
    FAILURE_REASONS,
    RULE_NAMES,
    RUNNER_REASONS,
    OutputRule,
    classify_output,
    record_rule_firing,
)


def test_rule_names_and_runner_reasons_are_disjoint():
    """The two halves may never name the same failure."""
    assert RULE_NAMES & RUNNER_REASONS == set()
    assert FAILURE_REASONS == RULE_NAMES | RUNNER_REASONS


def test_rule_names_covers_every_declared_outputrule():
    """A new OutputRule must join RULE_NAMES, or the column can't store it.

    Without this, adding a rule leaves `_record_telemetry` rejecting its name
    as "not in the namespace" and quietly filing the run as UNCLASSIFIED.
    """
    declared = {
        obj.name
        for _, obj in inspect.getmembers(output_rules)
        if isinstance(obj, OutputRule)
    }
    assert declared == set(RULE_NAMES), (
        f"OutputRule instances not in RULE_NAMES: {sorted(declared - set(RULE_NAMES))}; "
        f"RULE_NAMES with no rule: {sorted(set(RULE_NAMES) - declared)}"
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", "EMPTY_RESPONSE"),
        ("   ", "EMPTY_RESPONSE"),
    ],
)
def test_classify_output_only_ever_returns_namespace_members(text, expected):
    """Whatever classify_output decides is storable in failure_reason."""
    rule = classify_output(text)
    assert rule.name == expected
    assert rule.name in FAILURE_REASONS


def test_classifier_output_is_storable_for_varied_buffers():
    """Every classification of a real-shaped buffer lands in the namespace."""
    buffers = [
        "I have enough to construct the bull thesis. Let me emit the output.",
        '{"summary": "partial',
        '{"unrelated": 1}',
        "The company shows strong revenue growth across all segments.",
        "call:mcp__lazy-tool-service__get_sec_filings{ticker:WFC}",
    ]
    for buf in buffers:
        assert classify_output(buf).name in FAILURE_REASONS
    # The one class the classifier cannot see on its own.
    assert classify_output('{"a": 1}', wrong_shape=True).name in FAILURE_REASONS


def test_guardrail_firing_uses_the_same_string_as_failure_reason(monkeypatch):
    """`output_rule:<name>` must be exactly `failure_reason` with a prefix.

    This is the join. If `record_rule_firing` ever transformed the name (cased
    it, prefixed it twice, abbreviated it), a telemetry row and its firing
    would stop matching and every per-class rate would silently split in two.
    """
    captured = {}

    def _fake_record(guardrail, **kwargs):
        captured["guardrail"] = guardrail

    monkeypatch.setattr(
        "app.v3.telemetry.record_guardrail_firing", _fake_record, raising=True
    )
    rule = classify_output("")
    record_rule_firing(rule, agent_name="v3_junior_analyst", ticker="ASIC")

    assert captured["guardrail"] == f"output_rule:{rule.name}"
    assert captured["guardrail"].split(":", 1)[1] in FAILURE_REASONS


def test_runner_imports_only_namespace_members():
    """Every reason literal agent_runner can write is a namespace member.

    agent_runner imports its reasons by name from output_rules rather than
    spelling strings inline; this fails if someone re-inlines one.
    """
    from app.v3 import agent_runner

    for attr in ("SCHEMA_INVALID", "RUNNER_EXCEPTION", "REASON_TIMEOUT", "REASON_CANCELLED"):
        assert hasattr(agent_runner, attr), f"agent_runner lost {attr}"
        assert getattr(agent_runner, attr) in FAILURE_REASONS
