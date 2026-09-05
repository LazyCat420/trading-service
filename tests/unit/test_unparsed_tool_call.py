"""A native tool call the inference server did not parse is a TRANSPORT fault.

WHY THIS FILE EXISTS. On 2026-09-04 the Gold Spark was swapped to
`deepseek-v4-flash-vision-exp` served by **SGLang launched without
`--tool-call-parser deepseekv4`**. DeepSeek V4 emits its calls as DSML markup,
so every call came back inside `message.content` with `tool_calls` empty. In
`cycle-v3-1788565070` that produced, across 12 tickers:

    83 agent runs, every one at loops_used = 1
     0 rows in agent_tool_telemetry            (so "no tool failures")
    53 output_rule:NARRATED_NO_ARTIFACT firings, 52 of them "repaired"
    12 HOLD decisions written from the pre-collected briefing alone
       quality_score 80-88 on artifacts that did no research

Two separate defects made a transport outage look like a healthy cycle, and
this file pins both:

1.  `classify_output` called the DSML buffer NARRATED_NO_ARTIFACT, because the
    markup follows a prose preamble ("Let me check…") and the pseudo-tool-call
    regex is ANCHORED so it can never see markup that is not at position 0.
2.  Having named it narration, the runner ran the tool-less repair, which asks
    the model to "answer from the analysis you already performed" — from
    research that never happened — and books the result SUCCESS.

The fixtures below are REAL buffers, not paraphrases: `SGLANG_DSML_BUFFER` is
the stored `responsePayload.text` of the junior analyst's PURR run in that
cycle (prism `requests`, 2026-09-05T01:12Z). It is embedded rather than read
from the database or from git, so this test cannot expire when the row ages
out or when the fix lands.
"""

from __future__ import annotations

import inspect

import pytest

from app.v3 import agent_runner
from app.v3.output_rules import (
    NARRATED_NO_ARTIFACT,
    PROSE_REPORT,
    RULE_NAMES,
    TRUNCATED_JSON,
    UNPARSED_TOOL_CALL,
    classify_output,
)

# The real buffer, byte for byte. Note the FULLWIDTH VERTICAL LINE (U+FF5C) in
# the DSML markers — a regex written with the ASCII pipe alone does not match
# what DeepSeek actually emits, which is the kind of near-miss that would make
# this whole guard vacuous.
SGLANG_DSML_BUFFER = (
    "I'll analyze PURR. The prior research is thorough but dated 8/31. Key "
    "open questions: the HYPE token unlock status, the CFTC/Hyperliquid "
    "regulatory catalyst progress, and whether the price has moved since. Let "
    "me check the largest gaps — the dated catalyst status and any fresh "
    "news.\n\n"
    "<｜DSML｜tool_calls>\n"
    "<｜DSML｜invoke name=\"mcp__lazy-agent-service__get_finnhub_news\">\n"
    "<｜DSML｜parameter name=\"ticker\" string=\"true\">PURR"
    "</｜DSML｜parameter>\n"
    "</｜DSML｜invoke>\n"
    "</｜DSML｜tool_calls>"
)

#: The prose half of the SAME buffer. This is the negative control that makes
#: the positive one mean something: if the new rule matched on the narration
#: too, it would be relabelling every NARRATED run rather than isolating the
#: transport fault.
SGLANG_NARRATION_ONLY = SGLANG_DSML_BUFFER.split("<｜DSML")[0]


class TestTheTransportFaultIsNamed:
    def test_the_stored_sglang_buffer_is_a_transport_fault(self):
        assert classify_output(SGLANG_DSML_BUFFER) is UNPARSED_TOOL_CALL

    def test_the_narration_half_alone_is_still_narration(self):
        """The control. The old classifier saw ONLY this half."""
        assert classify_output(SGLANG_NARRATION_ONLY) is NARRATED_NO_ARTIFACT

    @pytest.mark.parametrize(
        "family,buffer",
        [
            ("deepseek-v4-dsml", SGLANG_DSML_BUFFER),
            (
                "deepseek-v4-bare-invoke",
                'Checking.\n<｜DSML｜invoke name="get_market_data">\n',
            ),
            ("deepseek-r1", "<|tool▁calls▁begin|><|tool▁call▁begin|>function"),
            (
                "qwen-hermes",
                'I will look it up.\n<tool_call>{"name": "get_market_data", '
                '"arguments": {"ticker": "NVDA"}}</tool_call>',
            ),
            ("mistral", '[TOOL_CALLS][{"name": "get_market_data"}]'),
            ("llama-3.1", '<|python_tag|>get_market_data(ticker="NVDA")'),
            ("llama-3.2", '<function=get_market_data>{"ticker": "NVDA"}</function>'),
        ],
    )
    def test_every_family_we_can_be_served_by(self, family, buffer):
        """One box swap is all it takes; the next one will not be DeepSeek."""
        assert classify_output(buffer) is UNPARSED_TOOL_CALL, family

    def test_the_qwen_shape_would_otherwise_be_swallowed_by_the_json_probe(self):
        """`<tool_call>{...}` is balanced JSON.

        Ordering proof, not a restatement: this buffer satisfies
        `_LOOKS_LIKE_JSON`, so a rule checked AFTER the JSON probes would never
        see it — it would land in UNCLASSIFIED and be repaired.
        """
        from app.utils.text_utils import _LOOKS_LIKE_JSON

        buf = '<tool_call>{"name": "get_market_data", "arguments": {}}</tool_call>'
        assert _LOOKS_LIKE_JSON.search(buf) is not None
        assert classify_output(buf) is UNPARSED_TOOL_CALL

    def test_the_anchored_pseudo_call_regex_cannot_see_it(self):
        """Why a new rule and not a widened old one.

        `_PSEUDO_TOOL_CALL_RE` uses `.match`, so it is anchored at position 0.
        The markup arrives after the model's preamble. This is the mechanism of
        the miss, asserted rather than described.
        """
        from app.v3.output_rules import _PSEUDO_TOOL_CALL_RE

        assert _PSEUDO_TOOL_CALL_RE.match(SGLANG_DSML_BUFFER) is None

    def test_ordinary_analysis_is_untouched(self):
        assert classify_output(
            "The company reported revenue of $4.2B, up 12% year over year, and "
            "the function of the CFO is unchanged."
        ) is PROSE_REPORT

    def test_a_truncated_artifact_is_still_truncation(self):
        assert classify_output('{"summary": "abc", "conf') is TRUNCATED_JSON

    def test_the_name_is_in_the_shared_namespace(self):
        """`v3_agent_telemetry.failure_reason` joins `v3_guardrail_firings` on
        these strings; a class outside the set cannot be counted."""
        assert UNPARSED_TOOL_CALL.name in RULE_NAMES


class TestTheRepairPassRefusesIt:
    def test_the_rule_declares_itself_a_transport_failure(self):
        assert UNPARSED_TOOL_CALL.transport_failure is True
        assert UNPARSED_TOOL_CALL.quote_previous is False

    def test_no_other_rule_claims_to_be_a_transport_failure(self):
        """A second transport class would silently disable repair for a shape
        that IS repairable."""
        from app.v3 import output_rules

        flagged = {
            r.name
            for r in vars(output_rules).values()
            if isinstance(r, output_rules.OutputRule) and r.transport_failure
        }
        assert flagged == {"UNPARSED_TOOL_CALL"}

    def test_the_runner_gates_the_repair_on_the_flag(self):
        """The gate lives in one branch of `run_v3_agent`; assert the source
        actually consults the flag before the repair call.

        Reading the source is deliberate: driving the whole agent loop needs a
        live prism, and a mocked loop would prove only that the mock was
        wired. What must not silently regress is the ORDER — the
        transport-failure branch must come before the repair branch, or the
        `elif` never runs.
        """
        src = inspect.getsource(agent_runner.run_v3_agent)
        gate = src.index("rule.transport_failure")
        repair = src.index("attempting tool-less")
        assert gate < repair, (
            "the transport-failure branch must precede the repair branch"
        )
        assert "elif artifact is None and final_text and bool(tool_whitelist):" in src
