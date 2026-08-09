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

# A tool call the model wrote as PROSE because nothing was left to execute it.
# Kept identical in shape to base_agent's copy of the same idea; the length cap
# that used to guard it is gone (see PSEUDO_TOOL_CALL below).
_PSEUDO_TOOL_CALL_RE = re.compile(
    r"^(?:call:|tool:|<tool_call>)?\s*"
    r"(?:mcp__[\w.-]+|[a-z][\w.-]*_[\w.-]*[\w])"
    r"[({\[]",
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
