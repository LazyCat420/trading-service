"""Tests for pinned doctrine documents and the agent that carries one.

Most of these are invariants ABOUT THE PROSE, not about the loader. That is
deliberate: a doctrine is the one part of a system prompt that will eventually
be written by a mining script rather than by hand, and mined speech happily
says things like "pull up the 10-K" or "check the EBITDA" — instructions naming
a tool the agent does not have, or a metric nothing computes. Both produce an
agent that follows an instruction into a dead end, which is the failure the
existing test_no_prompt_names_a_tool_the_agent_cannot_call invariant exists to
prevent for hand-written prompts.
"""

import re

import pytest

from app.quant import valuation_block as vb
from app.v3 import doctrine
from app.v3.agents import valuation_analyst as agent

DOCTRINE_NAME = "shkreli_valuation"


class TestLoader:
    def test_the_doctrine_loads(self):
        text = doctrine.load_doctrine(DOCTRINE_NAME)
        assert text
        assert DOCTRINE_NAME in doctrine.available()

    def test_it_is_under_the_size_ceiling(self):
        """Doctrine rides the SYSTEM half of the prompt on every ticker of
        every cycle, so its size is a per-run tax. MAX_SKILL_CHARS next door
        taught the lesson: a limit the code does not enforce is a suggestion,
        and skill docs grew straight through theirs."""
        assert len(doctrine.load_doctrine(DOCTRINE_NAME)) <= \
            doctrine.MAX_DOCTRINE_CHARS

    def test_an_oversized_doctrine_is_refused_not_truncated(self, monkeypatch):
        """Truncating would silently amputate the last rules — and the promote
        step orders by evidence weight, so the amputated ones are the
        best-supported."""
        monkeypatch.setattr(doctrine, "MAX_DOCTRINE_CHARS", 10)
        doctrine.load_doctrine.cache_clear()

        assert doctrine.load_doctrine(DOCTRINE_NAME) == ""

        doctrine.load_doctrine.cache_clear()

    def test_a_missing_doctrine_is_silent(self):
        """An agent run must never block on a doctrine — it still has the
        precomputed valuation block and its own method prompt."""
        assert doctrine.load_doctrine("no_such_doctrine_exists") == ""

    def test_a_name_cannot_escape_the_package(self):
        assert doctrine.load_doctrine("../../../etc/passwd") == ""


class TestTheDoctrineIsActionable:
    """Every rule must be executable by THIS agent against THIS data. A rule
    that cannot fire is not doctrine, it is decoration that costs prompt
    tokens on every run of every cycle."""

    @pytest.fixture
    def text(self):
        return doctrine.load_doctrine(DOCTRINE_NAME)

    def test_it_names_no_tool_the_agent_cannot_call(self, text):
        """The trap mined speech is most likely to spring."""
        from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

        granted = set(AGENT_TOOL_WHITELISTS[agent.AGENT_NAME])
        # Backticked identifiers that look like tool names, e.g. `screener_query`.
        mentioned = set(re.findall(r"`([a-z][a-z0-9_]{4,})`", text))
        known_tools = {
            name for name in mentioned
            if name in _ALL_TOOL_NAMES() and name not in _METRIC_NAMES()
        }

        assert known_tools <= granted, (
            f"doctrine names tools the agent cannot call: {known_tools - granted}"
        )

    def test_it_only_cites_metrics_the_block_emits(self, text):
        """Stops the doctrine drifting into asking for numbers nothing
        computes — which reads as an instruction and produces an invented
        value, the exact failure this whole seam exists to prevent."""
        cited = set(re.findall(r"`([a-z][a-z0-9_]{4,})`", text))
        metric_like = {c for c in cited if c.endswith(("_pct", "_ratio", "_cagr"))
                       or c in _METRIC_NAMES()}

        allowed = set(vb.VERIFIED_NUMERIC_FIELDS) | _ARTIFACT_FIELDS()
        assert metric_like <= allowed, (
            f"doctrine cites metrics nothing emits: {metric_like - allowed}"
        )

    def test_it_declares_itself_a_placeholder_until_mined(self, text):
        """The current file is hand-written, and saying so is load-bearing: it
        must not be read (or cited downstream) as a distillation of anyone's
        public commentary until the mine has actually run."""
        assert "PLACEHOLDER" in text

    def test_every_rule_is_numbered_for_doctrine_rules_applied(self, text):
        """`doctrine_rules_applied` reports rule IDS. If the rules are not
        addressable the field cannot be filled, and the doctrine's contribution
        becomes unmeasurable — the skill-gate failure repeated."""
        headings = re.findall(r"^## (\d+)\.", text, re.M)
        assert len(headings) >= 8
        assert headings == [str(i) for i in range(1, len(headings) + 1)]


class TestTheAgentModule:
    def test_the_doctrine_is_in_the_system_prompt(self):
        assert doctrine.load_doctrine(DOCTRINE_NAME) in agent.SYSTEM_PROMPT

    def test_the_whitelist_is_not_empty(self):
        """prism_registration reads an EMPTY whitelist as unscoped full
        catalog, not as 'no tools' — the inverse of the intent."""
        assert agent.TOOL_WHITELIST

    def test_the_prompt_survives_a_doctrine_failure(self, monkeypatch):
        monkeypatch.setattr(doctrine, "load_doctrine", lambda n: "")
        import importlib
        reloaded = importlib.reload(agent)
        try:
            assert reloaded.SYSTEM_PROMPT.strip()
            assert "Valuation Analyst" in reloaded.SYSTEM_PROMPT
        finally:
            monkeypatch.undo()
            importlib.reload(agent)

    def test_the_prompt_states_the_ebit_not_ebitda_caveat(self):
        """The agent sits between a block that says EV/EBIT and a desk that
        carries a vendor EV/EBITDA. Without this it will compare them."""
        assert "EV/EBIT is not EV/EBITDA" in agent.SYSTEM_PROMPT

    def test_the_prompt_forbids_zero_for_a_missing_metric(self):
        assert "do NOT write 0 for a missing metric" in agent.SYSTEM_PROMPT

    def test_not_assessable_is_reachable(self):
        """A NONE ON FILE block must produce NOT_ASSESSABLE, not a FAIR verdict
        manufactured out of having no data."""
        from app.v3.artifacts import VALUATION_REPORT_SCHEMA

        assert "NOT_ASSESSABLE" in agent.SYSTEM_PROMPT
        assert "NOT_ASSESSABLE" in \
            VALUATION_REPORT_SCHEMA["properties"]["verdict"]["enum"]


def _ALL_TOOL_NAMES() -> set:
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    return {t for tools in AGENT_TOOL_WHITELISTS.values() for t in tools}


def _METRIC_NAMES() -> set:
    return set(vb.VERIFIED_NUMERIC_FIELDS)


def _ARTIFACT_FIELDS() -> set:
    from app.v3.artifacts import VALUATION_REPORT_SCHEMA

    props = set(VALUATION_REPORT_SCHEMA["properties"])
    props |= set(
        VALUATION_REPORT_SCHEMA["properties"]["valuation_metrics"]["properties"]
    )
    return props
