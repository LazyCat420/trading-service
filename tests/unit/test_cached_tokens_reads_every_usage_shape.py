"""`cached_tokens` must read the prefix-cache figure whatever shape it arrives in.

MEASURED 2026-09-06 (Appendix K.3 item 4 of the trading-cycle audit).

The Gold Spark's vLLM `/metrics` said the server was serving **79.6% of prompt
tokens from the prefix cache** (3.72M of 4.67M; block hit rate 77.5%;
`enable_prefix_caching=True`). Our own telemetry said the cache did nothing:

    model                          runs   rows with cached_tokens > 0
    deepseek-v4-flash-0731          577   566   (83.3% of prompt tokens)
    nemotron35                      323     0
    deepseek-v4-flash-vision-exp    168     0
    GLM-5.3-Flash-EXL3               52     0

`base_agent` read exactly one key — `cacheReadInputTokens`, the Anthropic /
Bedrock spelling that prism forwards for DeepSeek and for nothing else. Every
local model reported a flat zero, so any prompt-trimming decision taken from
our telemetry was taken blind for the boxes we actually run.

Which key a provider uses is not something to guess at from a dashboard, so the
reader takes any of the known spellings AND logs the usage keys it actually saw
once per process — that line is what tells the next audit "prism does not
forward it" apart from "we were not reading it".
"""
from __future__ import annotations

import pytest

from app.agents.base_agent import extract_cached_tokens


class TestEveryKnownShape:
    @pytest.mark.parametrize(
        "usage,expected,label",
        [
            ({"cacheReadInputTokens": 12345}, 12345, "prism / bedrock (DeepSeek)"),
            ({"cached_tokens": 999}, 999, "flat snake_case"),
            ({"cache_read_input_tokens": 42}, 42, "snake_case bedrock"),
            (
                {"prompt_tokens_details": {"cached_tokens": 7777}},
                7777,
                "OpenAI / vLLM nested",
            ),
            (
                {"promptTokensDetails": {"cachedTokens": 31337}},
                31337,
                "OpenAI camelCase nested",
            ),
        ],
    )
    def test_the_figure_is_found(self, usage, expected, label):
        assert extract_cached_tokens(usage) == expected, label


class TestItDoesNotInventANumber:
    @pytest.mark.parametrize(
        "usage",
        [
            {},
            None,
            {"inputTokens": 1000},
            {"prompt_tokens_details": {}},
            {"prompt_tokens_details": None},
            {"cached_tokens": None},
            {"cached_tokens": "many"},
            {"cached_tokens": -5},
        ],
    )
    def test_absent_or_unusable_reads_zero(self, usage):
        assert extract_cached_tokens(usage) == 0

    def test_a_real_zero_is_still_zero(self):
        """A provider that genuinely reports no cache hit must not be rescued
        by a fallback key. Otherwise the metric could never go down."""
        assert extract_cached_tokens({"cacheReadInputTokens": 0}) == 0

    def test_the_first_populated_key_wins_and_they_are_not_summed(self):
        """Two spellings of the SAME figure appear together when a gateway
        passes both through. Adding them would double the cache hit."""
        usage = {"cacheReadInputTokens": 500, "cached_tokens": 500}
        assert extract_cached_tokens(usage) == 500


class TestTheRunHelperUsesIt:
    def test_base_agent_does_not_read_the_single_key_directly(self):
        """The seam, checked by AST: the payload must call the helper rather
        than reach for `cacheReadInputTokens` itself, or a second reader could
        drift back to the one-shape version."""
        import ast
        import inspect
        import pathlib

        from app.agents import base_agent

        tree = ast.parse(
            pathlib.Path(inspect.getsourcefile(base_agent)).read_text()
        )

        # Every `<something>.get("cacheReadInputTokens")` outside the helper.
        offenders = []
        helper = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "extract_cached_tokens":
                helper = node
        helper_lines = (
            range(helper.lineno, (helper.end_lineno or helper.lineno) + 1)
            if helper else range(0)
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
                continue
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and arg.value == "cacheReadInputTokens"
                    and node.lineno not in helper_lines
                ):
                    offenders.append(node.lineno)

        assert not offenders, (
            "base_agent reads cacheReadInputTokens directly at line(s) "
            f"{offenders} — local models report a different key and would "
            "read as a flat zero"
        )


# ── 2026-09-06: the shape prism ACTUALLY sends for every local-vLLM run ───────
# Verbatim fingerprint from the container log on cycle-v3-1788660665
# ("[BaseAgent] provider usage keys seen: ..."). Prism's accumulator
# (CostCalculator.ts createUsageAccumulator) initialises cacheReadInputTokens to
# 0 and always emits it, so a first-key-wins reader short-circuits on that 0 and
# never reaches the OpenAI-shaped fallback. On the cycle every GLM row read 0;
# the engine (GLM-5.3-Flash-EXL3) returns prompt_tokens_details: None, so today
# that is the truth — but the reader must not be the reason it stays 0 the day
# the engine starts reporting.
GLM_USAGE_UPDATE = {
    "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0, "inputTokens": 26069,
    "outputTokens": 412, "reasoningOutputTokens": 0, "requests": 1,
    "tokensPerSec": 18.4, "totalInputTokens": 26069,
}


def test_the_verbatim_glm_shape_reads_zero_today():
    assert extract_cached_tokens(dict(GLM_USAGE_UPDATE)) == 0


def test_an_always_present_zero_does_not_hide_a_populated_fallback():
    """The defect: cacheReadInputTokens=0 (accumulator default) beside a real
    prompt_tokens_details.cached_tokens must yield the real number."""
    usage = dict(GLM_USAGE_UPDATE, prompt_tokens_details={"cached_tokens": 19_712})
    assert extract_cached_tokens(usage) == 19_712


def test_a_populated_flat_key_still_wins_over_a_nested_one():
    usage = {"cacheReadInputTokens": 500, "prompt_tokens_details": {"cached_tokens": 999}}
    assert extract_cached_tokens(usage) == 500
