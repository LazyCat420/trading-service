"""The reasoning-leak canary must not accuse on a prefix alone.

2026-08-05: the tripwire fired on `v3_junior_analyst` for "Let me trace the
most load-bearing lead. The key story here is the AI capex/FCF..." — the
analyst's own report. Its error message names a cause ("the thinking-off flag
is not reaching the model"), that message was believed, and it was written
into the documentation as the root cause of an unrelated artifact-loss bug.

Measured the same day: 43/43 chat calls reached the vllm-shim carrying
enable_thinking=false, and the model reported reasoningOutputTokens=0.
"""

from app.services.prism_agent_caller import strip_reasoning_leak

PROSE = "Let me trace the most load-bearing lead. The key story here is the AI capex cycle, and the numbers behind it are what the desk should weigh."


def test_zero_reasoning_tokens_means_it_cannot_be_a_leak():
    text, leaked = strip_reasoning_leak(PROSE, "v3_junior_analyst", reasoning_tokens=0)
    assert leaked is False
    assert text == PROSE, "prose must pass through untouched"


def test_a_real_leak_still_trips_when_the_model_did_reason():
    _text, leaked = strip_reasoning_leak(PROSE, "v3_junior_analyst", reasoning_tokens=512)
    assert leaked is True, "genuine reasoning must still raise the alarm"


def test_unknown_usage_keeps_the_old_behaviour():
    """A caller that cannot see usage must not silently lose the tripwire."""
    _text, leaked = strip_reasoning_leak(PROSE, "v3_junior_analyst")
    assert leaked is True


def test_ordinary_output_is_never_flagged_either_way():
    body = '{"summary": "Revenue grew 14%.", "confidence": 72}'
    for tokens in (None, 0, 900):
        text, leaked = strip_reasoning_leak(body, "v3_quant_analyst", reasoning_tokens=tokens)
        assert leaked is False
        assert text == body


def test_empty_text_is_handled():
    for tokens in (None, 0, 5):
        assert strip_reasoning_leak("", "x", reasoning_tokens=tokens) == ("", False)
