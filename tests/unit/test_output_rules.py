"""Tests for the output-rule classifier (app/v3/output_rules.py).

The samples marked VERBATIM are the real buffers from `cycle_audit_log`, not
invented ones. A classifier tested only against text written by its own author
tests the author's imagination — the failure this codebase already recorded as
"a test that copies the logic cannot see it drift".
"""

import json

import pytest

from app.v3.output_rules import (
    classify_output,
    EMPTY_RESPONSE,
    NARRATED_NO_ARTIFACT,
    PROSE_REPORT,
    PSEUDO_TOOL_CALL,
    TRUNCATED_JSON,
    UNCLASSIFIED,
    WRONG_SHAPE,
)

# VERBATIM — v3_debate_judge on DE, cycle-v3-1786226138, 2026-08-08. The real
# buffer ran 21,473 chars; this is its head, which is what the classifier reads
# for markers. Note it contains no JSON at all.
NARRATION = (
    "I have both the bull argument and bear rebuttal. Let me also check if "
    "there's a bull defense turn (the bull's reply to the bear's rebuttal) on "
    "the whiteboard, since the rules require judging whether the bull answered "
    "the bear's points.\n\nI have the full bull argument and bear rebuttal."
)

# VERBATIM — the BaseAgent substitute for an empty stream.
EMPTY_SENTINEL = "Agent failed: empty response from v3_bear_agent"

# VERBATIM — prism's conversation store, WFC 2026-07-19.
PSEUDO_CALL = "call:mcp__lazy-tool-service__get_sec_filings{ticker:WFC}"

VALID_ARTIFACT = json.dumps({
    "summary": "Margins stable.",
    "key_findings": ["FCF positive"],
    "confidence": 65,
})


class TestTheClassesAreDistinct:
    def test_empty_sentinel_is_not_the_models_output(self):
        rule = classify_output(EMPTY_SENTINEL)
        assert rule is EMPTY_RESPONSE
        assert rule.quote_previous is False, (
            "the sentinel is this service's note that there was no reply — "
            "quoting it back asks the model to fix a sentence it never wrote"
        )

    def test_blank_buffer_is_an_empty_response(self):
        assert classify_output("   \n  ") is EMPTY_RESPONSE
        assert classify_output(None) is EMPTY_RESPONSE

    def test_pseudo_tool_call(self):
        assert classify_output(PSEUDO_CALL) is PSEUDO_TOOL_CALL

    def test_narration_without_an_artifact(self):
        assert classify_output(NARRATION) is NARRATED_NO_ARTIFACT

    def test_prose_report_is_not_narration(self):
        """A model that wrote a report made a different mistake than one that
        announced a report. Both get a JSON ask; only one is turn exhaustion."""
        report = (
            "## Recommendation: HOLD\n\nThe balance sheet carries 2.1x net "
            "leverage and the coverage ratio has compressed for three "
            "quarters running. Entry at spot is unattractive."
        )
        rule = classify_output(report)
        assert rule is PROSE_REPORT
        assert rule.exhausted is False

    def test_wrong_shape_wins_over_the_json_probes(self):
        """Valid JSON carrying none of the artifact's fields parses fine, so
        every text probe would misroute it. The caller supplies the verdict."""
        assert classify_output(VALID_ARTIFACT, wrong_shape=True) is WRONG_SHAPE


class TestTheFourHundredCharCap:
    """The regression this change exists for.

    base_agent required a pseudo tool call to be under 400 chars before it
    counted as budget exhaustion. 233 of 381 unparseable replies in the 7 days
    to 2026-08-08 were over 2k chars, so the majority of the wall was booked
    as `completed`.
    """

    def test_long_narration_is_exhaustion(self):
        long_narration = NARRATION + ("\nThe pre-collected data confirms: " * 200)
        assert len(long_narration) > 2000
        rule = classify_output(long_narration)
        assert rule is NARRATED_NO_ARTIFACT
        assert rule.exhausted is True, (
            "a model that narrated for 6k chars hit the same wall as one that "
            "narrated for 60"
        )

    def test_a_pseudo_call_after_a_long_preamble_still_counts(self):
        assert classify_output(
            PSEUDO_CALL + "\n" + ("x" * 3000)
        ) is PSEUDO_TOOL_CALL


class TestExhaustionIsNotClaimedForEveryFailure:
    """`exhausted` drives stop_reason, which sends the next reader to the turn
    budget. A label that fired for every failure would send them there for
    failures the budget cannot explain — a tripwire that names a cause it
    cannot see."""

    @pytest.mark.parametrize("rule", [
        EMPTY_RESPONSE, TRUNCATED_JSON, PROSE_REPORT, WRONG_SHAPE, UNCLASSIFIED,
    ])
    def test_non_turn_failures_do_not_claim_exhaustion(self, rule):
        assert rule.exhausted is False

    def test_a_valid_artifact_is_never_exhaustion(self):
        """stop_reason is derived on EVERY run, including successful ones."""
        assert classify_output(VALID_ARTIFACT).exhausted is False

    def test_truncation_is_a_token_ceiling_not_a_turn_ceiling(self):
        truncated = '{"summary": "Margins stable and the coverage ratio has'
        assert classify_output(truncated) is TRUNCATED_JSON
        assert TRUNCATED_JSON.exhausted is False


class TestTruncationDetectionIsStringAware:
    """A naive brace count calls every artifact containing a brace in its prose
    truncated, and misses a real truncation that happens to balance.

    Every fixture here is chosen so that `count("{") != count("}")` gives the
    WRONG answer. A balanced sample like `"{x}"` would pass under both
    implementations and prove nothing — verified by sabotage: the naive counter
    survived exactly that test.
    """

    def test_an_unbalanced_brace_inside_a_string_does_not_open_an_object(self):
        # Naive count: 2 '{' vs 1 '}' -> "truncated". It is complete.
        complete = '{"thesis": "the payout formula is { per share", "c": 1}'
        assert classify_output(complete) is not TRUNCATED_JSON

    def test_an_escaped_quote_does_not_end_the_string(self):
        # The escaped quotes must not re-open the string and swallow the
        # closing brace. Naive count is balanced here, so this one guards the
        # escape handling rather than the counting.
        complete = '{"thesis": "he called it \\"cheap\\" at 12x", "c": 1}'
        assert classify_output(complete) is not TRUNCATED_JSON

    def test_a_cut_inside_a_string_is_truncation(self):
        assert classify_output('{"thesis": "the coverage ratio has com') is TRUNCATED_JSON

    def test_a_truncation_whose_braces_happen_to_balance(self):
        # Naive count: 1 '{' vs 1 '}' -> "complete". The '}' is inside a
        # string, so the object never closed.
        assert classify_output('{"note": "closes with }", "action": ') is TRUNCATED_JSON

    def test_a_nested_object_cut_at_depth_is_truncation(self):
        cut = '{"a": {"b": {"c": 1}, "d": 2'
        assert classify_output(cut) is TRUNCATED_JSON

    def test_trailing_prose_after_a_complete_object_is_not_truncation(self):
        """Models append a sign-off after the JSON. The object closed; whatever
        went wrong, it was not the token ceiling."""
        assert classify_output(
            VALID_ARTIFACT + "\n\nLet me know if you need more detail."
        ) is not TRUNCATED_JSON


class TestUnclassifiedIsARealAnswer:
    def test_balanced_json_that_still_failed_is_named_unclassified(self):
        """Not pooled into a neighbouring class: a rising UNCLASSIFIED count is
        the signal that a new failure shape needs its own rule."""
        assert classify_output('{"a": 1, "b": [2, 3]}') is UNCLASSIFIED

    def test_unclassified_carries_no_directive(self):
        assert UNCLASSIFIED.directive == ""


class TestEveryRuleIsUsable:
    @pytest.mark.parametrize("rule", [
        EMPTY_RESPONSE, PSEUDO_TOOL_CALL, NARRATED_NO_ARTIFACT,
        TRUNCATED_JSON, PROSE_REPORT, WRONG_SHAPE,
    ])
    def test_named_rules_say_something_specific(self, rule):
        assert rule.name and rule.name.isupper()
        assert len(rule.directive) > 40, (
            "a directive short enough to be generic is the thing this replaces"
        )

    def test_directives_are_distinct(self):
        rules = [
            EMPTY_RESPONSE, PSEUDO_TOOL_CALL, NARRATED_NO_ARTIFACT,
            TRUNCATED_JSON, PROSE_REPORT, WRONG_SHAPE,
        ]
        assert len({r.directive for r in rules}) == len(rules)


# ── PROVIDER_ERROR (added 2026-08-09) ────────────────────────────────────

# VERBATIM — the harness apology the V3 Delta Analyst received on GEN,
# cycle-v3-1786297004, after Gold Spark's queue starved its call for 300s.
PROVIDER_APOLOGY = (
    "⚠️ Error: The model provider encountered an error on iteration 1: "
    "Provider stream stalled: no data received for 300s. The conversation "
    "history up to this point has been preserved. You can retry your "
    "request, or try a different model/provider if this persists."
)


def test_provider_apology_classifies_as_provider_error():
    from app.v3.output_rules import PROVIDER_ERROR

    assert classify_output(PROVIDER_APOLOGY) is PROVIDER_ERROR


def test_provider_error_never_quotes_the_buffer_back():
    """The buffer is prism's apology, not the model's work — quoting it back
    re-creates the bear-argues-with-its-own-error-message shape from Ch.29."""
    from app.v3.output_rules import PROVIDER_ERROR

    assert PROVIDER_ERROR.quote_previous is False
    assert PROVIDER_ERROR.exhausted is False


def test_markdown_bold_variant_still_matches():
    # prism's UI renders "⚠️ **Error:**" — the classifier must not depend on
    # the exact decoration around the i18n sentence.
    text = (
        "⚠️ **Error:** The model provider encountered an error on iteration 2: "
        "`API error: 502`. The conversation history up to this point has been "
        "preserved."
    )
    from app.v3.output_rules import PROVIDER_ERROR

    assert classify_output(text) is PROVIDER_ERROR


def test_an_analysis_that_mentions_a_provider_error_is_not_one():
    """Position guard: the marker deep in a long reply is the model WORKING
    (e.g. summarising an incident), not the transport failing."""
    filler = "The desk reviewed infrastructure reliability this week. " * 20
    text = (
        filler
        + "Notably, the model provider encountered an error on iteration 1 "
        + "during Saturday's cycle, which the team traced to queue saturation."
    )
    from app.v3.output_rules import PROVIDER_ERROR

    assert classify_output(text) is not PROVIDER_ERROR
