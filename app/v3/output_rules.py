"""Named rules over an agent's final text: what went wrong, and what to say back.

WHY THIS EXISTS. 381 agent runs in the 7 days to 2026-08-08 produced no
parseable artifact (`cycle_audit_log`, "unparseable output for"). They are not
one bug wearing one costume:

    233  >2k chars   the model NARRATED its next tool call and the run ended
                     ("I have both the bull argument and bear rebuttal. Let me
                     also check the whiteboard..." — 21,473 chars, v3_debate_judge,
                     cycle-v3-1786226138). No JSON anywhere in the buffer.
     84  100-2k      mixed: truncated artifacts and prose reports
     64  <100        the empty-response sentinel and pseudo tool calls

Every one of them took the SAME generic repair prompt ("Your previous reply
could not be parsed") and every one of them was counted the same way: not at
all. A failure class that cannot be named cannot be counted, and a class that
cannot be counted cannot be shown to have improved — the lesson this desk has
now learned from guardrails, from `hold_reason`, and from the bear's
`preferred_alternative`, which is exactly why that field has five states and
never pools them.

So: classify the buffer, name the class, say something class-specific back to
the model, and record the firing where it can be summed.

WHAT THIS IS NOT. Not a parser. It runs only once parsing has already failed
(or produced the wrong shape), and it never returns an artifact — it returns a
label and the sentence to put in the repair ask. Rewriting the model's buffer
into an artifact is what `_malformed_fallback` declines to do, for the reason
recorded there: regex-scraping a broken buffer manufactures fields the model
never emitted.

THE ONE DEFINITION RULE. "Does this buffer contain JSON" has exactly one
definition in this codebase — `text_utils._LOOKS_LIKE_JSON`, the regex the
prose fallback already gates on. A second copy here would drift from it, and
the two would disagree about the same buffer: the parser would decline a
response as "contains JSON" while these rules called it narration and asked the
model to start writing JSON it had already written. Same defect as the
duplicate candidate pool that `substitute.py` warns about, in a different file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

# The one definition of "the model was writing JSON, whatever else is in the
# buffer". Private in text_utils and deliberately imported rather than copied —
# see THE ONE DEFINITION RULE above.
from app.utils.text_utils import _LOOKS_LIKE_JSON

logger = logging.getLogger(__name__)


# The BaseAgent substitute for an empty stream. Matched as a PREFIX, not by
# equality, because the agent name is interpolated into it:
#     content = f"Agent failed: empty response from {agent_name}"
# This string is not the model's output. It is this service's own note that
# there WAS no output, and quoting it back at the model as "your previous
# reply" is how a 47-char repair ask asks a model to fix a sentence it never
# wrote (58 times on v3_bear_agent alone in 7 days).
_EMPTY_SENTINEL_PREFIX = "Agent failed: empty response from "

# The SDK's own exhaustion sentinel. Prism never sends it (see base_agent), but
# a direct-SDK path still can, and it is unambiguous when it appears.
_SDK_EXHAUSTION_SENTINEL = "Max iterations reached without a final answer."

# Prism's harness apology, injected into the conversation when a provider
# pass throws (locales/en/harness.json "providerError"). Matched only near
# the START of the buffer: an analysis that merely QUOTES a provider error it
# read somewhere is the model working, not the transport failing.
_PROVIDER_ERROR_MARKER = "the model provider encountered an error on iteration"
_PROVIDER_ERROR_WINDOW = 300

# A tool call the model wrote as PROSE because nothing was left to execute it.
# Kept identical in shape to base_agent's copy of the same idea; the length cap
# that used to guard it is gone (see PSEUDO_TOOL_CALL below).
_PSEUDO_TOOL_CALL_RE = re.compile(
    r"^(?:call:|tool:|<tool_call>)?\s*"
    r"(?:mcp__[\w.-]+|[a-z][\w.-]*_[\w.-]*[\w])"
    r"[({\[]",
)

# A tool call the transport failed to PARSE. Not the model going off-script:
# the model emitted its provider's native tool-call syntax and the inference
# server handed it back as message content because it was launched without a
# tool-call parser for that family.
#
# MEASURED 2026-09-04/05, cycle-v3-1788565070: the Gold Spark was swapped to
# `deepseek-v4-flash-vision-exp` served by **SGLang with no
# `--tool-call-parser deepseekv4`**. Every one of 83 agent runs came back
# carrying `<|DSML|tool_calls><|DSML|invoke name="mcp__...">` in the TEXT with
# `tool_calls` empty and prism's `toolsUsed` false. `classify_output` matched
# the narration markers ("let me ", "i'll ") and called all 53 firings
# NARRATED_NO_ARTIFACT, the tool-less repair pass then produced an artifact
# from the pre-collected briefing alone, and the runs were booked SUCCESS with
# quality 80-88. Zero rows reached `agent_tool_telemetry` for the whole cycle,
# so every instrument read "no tool failures".
#
# SEARCHED, not matched from the start: the markup follows the model's prose
# preamble, so an anchored regex (which is what _PSEUDO_TOOL_CALL_RE is) can
# never see it. Checked BEFORE the JSON probes because Qwen/Hermes wrap their
# call in `<tool_call>{...}` — balanced JSON that `_LOOKS_LIKE_JSON` would
# claim, sending a transport failure to UNCLASSIFIED.
#
# One entry per family we can actually be served by; adding a family here is
# cheaper than the four days this cost.
_UNPARSED_TOOL_CALL_RE = re.compile(
    r"<[|\uff5c]DSML[|\uff5c]tool_calls>"      # DeepSeek V4 (and V3.2)
    r"|<[|\uff5c]DSML[|\uff5c]invoke\s+name="  # ...a lone invoke, no wrapper
    r"|<\|tool\u2581calls\u2581begin\|>"        # DeepSeek R1
    r"|<tool_call>\s*\{"                        # Qwen / Hermes
    r"|\[TOOL_CALLS\]"                          # Mistral
    r"|<\|python_tag\|>"                        # Llama 3.1
    r"|<function=[\w.-]+>",                     # Llama 3.2 / xLAM
    re.IGNORECASE,
)

# First-person planning. The tell of the 233-run majority class: the model is
# announcing the step it is ABOUT to take, which means the buffer is a
# transcript of intent rather than a report. Lowercased before matching.
#
# These are deliberately narrow. "check" or "verify" alone would match ordinary
# analytical prose ("we check the coverage ratio"); a first-person subject
# followed by an intention verb is what separates narration from a report.
_NARRATION_MARKERS = (
    "let me ",
    "let's check",
    "i now have",
    "i have the",
    "i have both",
    "i'll ",
    "i will ",
    "next, i",
    "first, i",
    "i need to check",
    "i should check",
)


@dataclass(frozen=True)
class OutputRule:
    """A named failure class plus the remediation to inject.

    `directive` is the sentence(s) added to the repair ask — the analog of a
    system reminder fired the moment the model goes off-script. `quote_previous`
    decides whether the failed buffer is shown back to the model at all; for
    EMPTY_RESPONSE it must not be, because the buffer is ours, not the model's.
    `exhausted` says this shape means the run hit its turn wall, which is what
    `stop_reason` reports.
    """

    name: str
    directive: str
    quote_previous: bool = True
    exhausted: bool = False
    #: The TRANSPORT failed, not the model. The tool-less repair pass must not
    #: run for these: re-asking a model that never got its tool results to
    #: "answer from what you already found" produces an artifact built from the
    #: prompt alone, and the run is then recorded as a SUCCESS. Failing loudly
    #: is the point — the desk degrades where somebody can see it.
    transport_failure: bool = False


# ── The rules ───────────────────────────────────────────────────────────

EMPTY_RESPONSE = OutputRule(
    name="EMPTY_RESPONSE",
    # No directive about "your previous reply": there wasn't one. The known
    # causes are upstream (prism's injected minP against a speculative-decoding
    # box, a model swap, a zero-token stream), so the honest ask is a fresh one.
    directive=(
        "Your previous call returned no content at all. Answer now with the "
        "artifact and nothing else."
    ),
    quote_previous=False,
)

PROVIDER_ERROR = OutputRule(
    name="PROVIDER_ERROR",
    # The buffer is prism's harness apology (locales/en/harness.json
    # "providerError"), injected when the PROVIDER pass threw — a 300s stall,
    # a 502 from the shim, a dead box. The model never answered, so like
    # EMPTY_RESPONSE the buffer must not be quoted back (2026-08-09: the bear
    # spent a cycle arguing with its own error message). Until this rule
    # existed these failures were counted as the MODEL narrating — 12 stalls
    # and ~90 fetch-failures over two days booked against agent behaviour
    # when the request never reached a GPU.
    directive=(
        "Your previous call failed in the transport layer before the model "
        "could answer — nothing you wrote was lost because nothing was "
        "written. Answer now with the artifact and nothing else."
    ),
    quote_previous=False,
)

PSEUDO_TOOL_CALL = OutputRule(
    name="PSEUDO_TOOL_CALL",
    directive=(
        "Your previous reply was a tool call written as plain text. There is "
        "no tool runtime left in this turn — nothing will execute it. Answer "
        "from what you already found."
    ),
    exhausted=True,
)

NARRATED_NO_ARTIFACT = OutputRule(
    name="NARRATED_NO_ARTIFACT",
    directive=(
        "Your previous reply described the work you were ABOUT to do and never "
        "emitted the artifact. Do not plan, do not announce a next step, do "
        "not call tools. Emit the artifact itself as your entire reply."
    ),
    exhausted=True,
)

UNPARSED_TOOL_CALL = OutputRule(
    name="UNPARSED_TOOL_CALL",
    directive=(
        "Your previous reply contained a tool call in your model's native "
        "syntax, which the inference server returned as text instead of "
        "executing. This is a transport fault on our side, not yours."
    ),
    # Never shown back to the model: the repair pass does not run for this
    # class at all (see `transport_failure`), so the directive exists to name
    # the class in the log and the firing row, not to ask for a retry.
    quote_previous=False,
    transport_failure=True,
)

TRUNCATED_JSON = OutputRule(
    name="TRUNCATED_JSON",
    # A truncated artifact is a max_tokens problem, NOT a turn-budget one, so
    # `exhausted` stays False — labelling it max_iterations would send the next
    # reader to AGENT_BUDGET_OVERRIDES for a ceiling that is not the one that
    # was hit.
    directive=(
        "Your previous reply began the artifact but was cut off before its "
        "closing brace. Re-emit it COMPLETE. Keep every required field and cut "
        "the length of the free-text fields to fit."
    ),
)

PROSE_REPORT = OutputRule(
    name="PROSE_REPORT",
    directive=(
        "Your previous reply was a written report. The desk cannot read prose "
        "— it reads one JSON object. Convert what you already wrote into the "
        "artifact."
    ),
)

WRONG_SHAPE = OutputRule(
    name="WRONG_SHAPE",
    directive=(
        "Your previous reply was valid JSON but not this artifact — it carried "
        "none of the required fields. Emit the artifact named below, at the top "
        "level, not nested inside another object."
    ),
)

UNCLASSIFIED = OutputRule(
    name="UNCLASSIFIED",
    # The pre-existing behaviour, kept as a real rule rather than a None so
    # every repair is counted under some name. A rising UNCLASSIFIED count is
    # the signal that a new failure shape has appeared and needs its own rule.
    directive="",
)


# ── The failure-reason namespace ────────────────────────────────────────
#
# THE ONE DEFINITION RULE, applied to failure NAMES. `v3_agent_telemetry` now
# carries a `failure_reason` column, and the obvious way to fill it is a fresh
# enum written next to the writer — which is how a codebase ends up with two
# taxonomies that disagree about the same run. A second EMPTY_RESPONSE, decided
# by a different code path from `classify_output`, would let a telemetry row
# say EMPTY_RESPONSE while the `output_rule:EMPTY_RESPONSE` firing for the same
# run said something else, and no query could tell you which one was lying.
#
# So the column reuses THESE names. Where a rule fired, `failure_reason` IS
# `rule.name`, so it joins straight onto the `output_rule:` rows in
# `v3_guardrail_firings` — one producer (`classify_output`), one vocabulary.
#
# The reasons below are the complement: failures where classification never ran
# because there was no buffer to classify. They are deliberately disjoint from
# every rule name, and `_assert_disjoint()` fails at import if anyone breaks
# that — the moment the two sets overlap, the join above starts double-counting.
RULE_NAMES = frozenset({
    EMPTY_RESPONSE.name, PROVIDER_ERROR.name, PSEUDO_TOOL_CALL.name,
    NARRATED_NO_ARTIFACT.name, TRUNCATED_JSON.name, PROSE_REPORT.name,
    WRONG_SHAPE.name, UNCLASSIFIED.name, UNPARSED_TOOL_CALL.name,
})

#: The artifact PARSED — so no rule fired — but failed schema validation.
SCHEMA_INVALID = "SCHEMA_INVALID"
#: The run never came back: wall-clock timeout, operator stop, runner crash.
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
RUNNER_EXCEPTION = "RUNNER_EXCEPTION"

RUNNER_REASONS = frozenset({SCHEMA_INVALID, TIMEOUT, CANCELLED, RUNNER_EXCEPTION})

FAILURE_REASONS = RULE_NAMES | RUNNER_REASONS


def _assert_disjoint() -> None:
    """Fail at import if the two halves of the namespace ever collide."""
    overlap = RULE_NAMES & RUNNER_REASONS
    if overlap:
        raise AssertionError(
            f"failure_reason namespace collision: {sorted(overlap)}. A name "
            f"cannot be both an OutputRule class and a runner reason — "
            f"v3_agent_telemetry.failure_reason joins v3_guardrail_firings on "
            f"these strings."
        )


_assert_disjoint()


def _json_is_truncated(text: str) -> bool:
    """True when an object opens and its braces never balance.

    String-aware: a brace inside a quoted value must not move the depth, and
    `\\"` inside a string must not end it. A naive `count("{") != count("}")`
    calls every artifact containing "}" in a thesis string truncated.
    """
    start = text.find("{")
    if start < 0:
        return False

    depth = 0
    in_string = False
    escaped = False
    for ch in text[start:]:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                # A complete object exists in the buffer. Whatever failed, it
                # was not truncation.
                return False
    return depth > 0 or in_string


def classify_output(text: str | None, *, wrong_shape: bool = False) -> OutputRule:
    """Name the failure class of an unparseable (or wrong-shaped) agent reply.

    `wrong_shape` is passed by the caller for the one class this function
    cannot see: a buffer that parsed cleanly into JSON carrying none of the
    artifact's fields. That judgement needs the artifact schema, which lives in
    the runner.
    """
    stripped = (text or "").strip()

    if not stripped or stripped.startswith(_EMPTY_SENTINEL_PREFIX):
        return EMPTY_RESPONSE

    if stripped == _SDK_EXHAUSTION_SENTINEL:
        return PSEUDO_TOOL_CALL

    # Before every content probe: a buffer that OPENS with prism's provider
    # apology is a transport failure wearing a reply's clothes. Checked by
    # position, not mere presence — see _PROVIDER_ERROR_MARKER.
    if _PROVIDER_ERROR_MARKER in stripped[:_PROVIDER_ERROR_WINDOW].lower():
        return PROVIDER_ERROR

    # Checked before the JSON probes: `wrong_shape` means parsing SUCCEEDED, so
    # the buffer does contain balanced JSON and every probe below would
    # misroute it.
    if wrong_shape:
        return WRONG_SHAPE

    # The length cap this check used to carry (<400 chars) is deliberately
    # gone. It was there to avoid matching prose, but the anchored regex
    # already requires the line to OPEN with an identifier butting against a
    # bracket. A model that narrates for 3k chars and then signs off with one
    # pseudo call still hit the same wall as one that emitted only the call.
    # Before BOTH the pseudo-call probe and the JSON probes. A native tool call
    # returned as text is a transport fault and is not repairable by re-asking
    # the model; every probe below would mislabel it (the DeepSeek shape as
    # narration, the Qwen shape as JSON).
    if _UNPARSED_TOOL_CALL_RE.search(stripped):
        return UNPARSED_TOOL_CALL

    if _PSEUDO_TOOL_CALL_RE.match(stripped):
        return PSEUDO_TOOL_CALL

    has_json = _LOOKS_LIKE_JSON.search(stripped) is not None

    if has_json:
        if _json_is_truncated(stripped):
            return TRUNCATED_JSON
        # JSON is present, balanced, and still did not parse or did not
        # validate. No rule owns this yet — say so rather than guessing.
        return UNCLASSIFIED

    lowered = stripped.lower()
    if any(marker in lowered for marker in _NARRATION_MARKERS):
        return NARRATED_NO_ARTIFACT

    return PROSE_REPORT


def record_rule_firing(
    rule: OutputRule,
    *,
    agent_name: str,
    ticker: str = "",
    cycle_id: str = "",
    chars: int = 0,
    repaired: bool | None = None,
) -> None:
    """Persist one firing so the class rate is queryable.

    Rides the existing `v3_guardrail_firings` table under an `output_rule:`
    namespace rather than adding a second telemetry table with its own boot
    migration and its own way to go stale. Query:

        SELECT guardrail, detail->>'agent', count(*)
        FROM v3_guardrail_firings
        WHERE guardrail LIKE 'output_rule:%'
        GROUP BY 1, 2 ORDER BY 3 DESC;

    Fail-open, like every other probe here: telemetry must never break the
    pipeline it observes.
    """
    try:
        from app.v3.telemetry import record_guardrail_firing

        record_guardrail_firing(
            f"output_rule:{rule.name}",
            ticker=ticker,
            cycle_id=cycle_id,
            detail={
                "agent": agent_name,
                "chars": chars,
                # None until the repair pass has run — a firing recorded at
                # classification time and a firing recorded after repair are
                # different rows only in this field, so it must be nullable
                # rather than defaulted to False.
                "repaired": repaired,
            },
        )
    except Exception as e:  # noqa: BLE001 — never block the pipeline
        logger.debug("[OutputRules] firing not recorded (non-fatal): %s", e)
