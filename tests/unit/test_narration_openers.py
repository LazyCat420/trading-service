"""A reply that says it is MISSING an input is not a written report.

Measured 2026-09-12 over the 486 `[V3Runner] Failed to parse artifact` HEADs
in `cycle_audit_log`: ten classified PROSE_REPORT, and three of those are
`v3_bull_defense` announcing it never received the section it was asked to
answer. PROSE_REPORT's directive is "Your previous reply was a written
report... Convert what you already wrote into the artifact" — addressed to a
reply with nothing in it to convert — and because `base_agent` infers
`stop_reason == "max_iterations"` from `classify_output(...).exhausted`, the
turn-wall count was short by every one of them.

Also pinned here: `ZERO_USAGE_MIN_CHARS`, which was examined in the same pass
and deliberately LEFT ALONE. See the test at the bottom for the numbers.
"""

from __future__ import annotations

import pytest

from app.v3.output_rules import (
    NARRATED_NO_ARTIFACT,
    PROSE_REPORT,
    ZERO_USAGE_MIN_CHARS,
    classify_output,
    was_cut_off,
)

#: Verbatim, from `cycle_audit_log` HEADs and `llm_audit_logs.raw_response`.
REAL_OPENERS = [
    # v3_bull_defense, 88 chars. The reply named in the 30-day measurement.
    "I need to see the full bear rebuttal and judge sections to answer every point precisely.",
    # v3_bull_defense, twice, 57 chars.
    "I need to read the Bear's rebuttal to answer it properly.",
    # technical_analyst, llm_audit_logs.
    "I need to gather current market context and technical indicators to ensure my "
    "analysis is properly validated.",
    "I need to analyze this OHLCV data comprehensively before generating the overlay "
    "specification.",
]


@pytest.mark.parametrize("text", REAL_OPENERS)
def test_a_reply_that_is_waiting_on_an_input_is_narration(text):
    """RED before the fix: all four came back PROSE_REPORT."""
    rule = classify_output(text)

    assert rule.name == NARRATED_NO_ARTIFACT.name, (
        f"{text[:60]!r} was classified {rule.name}"
    )


@pytest.mark.parametrize("text", REAL_OPENERS)
def test_the_turn_wall_is_now_counted(text):
    """`stop_reason` is read off `.exhausted`; NARRATED carries it, PROSE does not."""
    assert classify_output(text).exhausted is True
    assert PROSE_REPORT.exhausted is False


def test_a_genuine_report_that_merely_mentions_needing_more_is_still_a_report():
    """The reason these markers are POSITIONAL and not members of the tuple.

    This is a real published bear artifact's prose — "I need to see cash capex
    peak before I get comfortable" appears mid-report in
    `pipeline_trace_blobs`. Matched anywhere in the buffer, it would relabel a
    complete written analysis as narration and send it the wrong directive.
    """
    report = (
        "Amazon's retail margins ran at 39.4%, and retail cash flow can carry the "
        "capex weight even if the Fed stays higher for longer. Oracle offers more "
        "upside if rates fall and the RPO converts to revenue on schedule, but "
        "I need to see cash capex peak before I get comfortable. Oracle offers "
        "higher torque to a Fed pivot and OCI execution, while Amazon's "
        "diversification offers more insulation if rates stay elevated."
    )

    assert classify_output(report).name == PROSE_REPORT.name


def test_zero_usage_min_chars_is_deliberately_unchanged():
    """The guard stays blind below 120 chars, ON PURPOSE.

    Measured 2026-09-12 over all 12,101 non-empty `llm_audit_logs.raw_response`
    rows: 24 carry `tokens_used == 0`. Of the 1,226 replies shorter than 120
    chars, exactly ONE does (0.08%) — against a 0.20% base rate over the whole
    corpus, so short replies are not enriched for zero usage at all, and
    lowering the floor would admit one buffer in twelve hundred.

    That one buffer is also the only reason to keep the floor where it is
    rather than remove the char gate entirely: it IS a truncation ("...or are
    waiting", no terminal punctuation, voice_quant, 87 chars). The replies this
    investigation was about are the opposite shape — complete sentences with a
    terminal period at 147k prompt tokens — and they carry a real token count,
    so the zero-usage branch is unreachable for them. Dropping the char gate
    could only start calling them truncated.
    """
    assert ZERO_USAGE_MIN_CHARS == 120

    complete_but_short = REAL_OPENERS[0]
    assert len(complete_but_short) < ZERO_USAGE_MIN_CHARS
    assert complete_but_short.endswith(".")

    # Even with the worst-case envelope — a provider reporting no output at
    # all — a complete short sentence must not be called a truncation.
    assert was_cut_off({"tokens_used": 0}, complete_but_short) is False
