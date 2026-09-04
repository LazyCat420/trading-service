"""Unit tests for prism agent registry mappings and fallback resolution."""

from app.services.prism_agent_registry import resolve_agent_id


def test_autoresearch_reflection_maps_to_synthesizer():
    assert resolve_agent_id("autoresearch_reflection") == "CUSTOM_SYNTHESIZER_AGENT"
    assert resolve_agent_id("reflection") == "CUSTOM_SYNTHESIZER_AGENT"


def test_auditors_map_to_meta_audit_agent():
    assert resolve_agent_id("auditor_1") == "CUSTOM_META_AUDIT_AGENT"
    assert resolve_agent_id("auditor_2") == "CUSTOM_META_AUDIT_AGENT"
    assert resolve_agent_id("auditor_3") == "CUSTOM_META_AUDIT_AGENT"
    assert resolve_agent_id("chief_auditor") == "CUSTOM_META_AUDIT_AGENT"
    assert resolve_agent_id("evaluator") == "CUSTOM_META_AUDIT_AGENT"


# ── Every literal caller's persona, pinned ────────────────────────────────
"""bed708d mapped auditor_1..3 / chief_auditor / autoresearch_reflection after
they were found running as the data janitor. It fixed the five names someone
noticed; it did not ask how many others were in the same position.

Ten are. `resolve_agent_id` ends in an unconditional
`base_agent = "CUSTOM_SYSTEM_JANITOR_AGENT"`, so an unmapped name gets a
persona nobody chose for it and the call still succeeds — there is no error to
notice, only a different prompt. The registry now logs that fallback once per
name; these tests keep the current answer from drifting silently.

Re-mapping the ten is a model-behaviour change and an operator decision, not a
refactor — memory_consolidator, judge_evaluator, strategy_evaluator and
query_decomposer are evaluative roles wearing a janitor's persona, while
translator and audit_worker plausibly belong there. Recorded as an open item
rather than changed here.
"""

import ast
import pathlib

from app.services.prism_agent_registry import AGENT_ID_MAP, resolve_agent_id

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

#: name at a call site -> the persona it resolves to TODAY.
EXPECTED_RESOLUTION = {
    "": "CUSTOM_MARKET_ALPHA",
    "CUSTOM_CONSOLIDATOR_AGENT": "CUSTOM_TRADING_CYCLE_ANALYSIS_AGENT",
    "CUSTOM_MEMORY_BRIEFER_AGENT": "CUSTOM_TRADING_CYCLE_ANALYSIS_AGENT",
    "CUSTOM_MORNING_BRIEFING_AGENT": "CUSTOM_TRADING_CYCLE_ANALYSIS_AGENT",
    "CUSTOM_V3_JUNIOR_ANALYST": "CUSTOM_V3_JUNIOR_ANALYST",
    "audit_worker": "CUSTOM_SYSTEM_JANITOR_AGENT",
    "autoresearch_reflection": "CUSTOM_SYNTHESIZER_AGENT",
    "chief_auditor": "CUSTOM_META_AUDIT_AGENT",
    "equation_lab": "CUSTOM_SYSTEM_JANITOR_AGENT",
    "flash_briefing": "CUSTOM_TRADING_CYCLE_ANALYSIS_AGENT",
    "grounding_judge": "CUSTOM_TRADING_CYCLE_ANALYSIS_AGENT",
    "judge_evaluator": "CUSTOM_SYSTEM_JANITOR_AGENT",
    "memory_briefer": "CUSTOM_SYSTEM_JANITOR_AGENT",
    "memory_consolidator": "CUSTOM_SYSTEM_JANITOR_AGENT",
    "morning_briefing_analyst": "CUSTOM_TRADING_CYCLE_ANALYSIS_AGENT",
    "query_decomposer": "CUSTOM_SYSTEM_JANITOR_AGENT",
    "skillopt_optimizer": "CUSTOM_SYSTEM_JANITOR_AGENT",
    "strategy_evaluator": "CUSTOM_SYSTEM_JANITOR_AGENT",
}

#: The subset with no explicit mapping — running as the fallback persona.
# "Translator"/"translator" LEFT this set on 2026-09-03 — they are no longer
# call_prism_agent callers at all. The foreign-feed translator moved to
# chat_toolless (`/chat`): through `/agent` it was paying ~25,900 input tokens
# for a three-sentence translation, because prism attaches the MCP catalog and
# injects the persona's memories server-side, and it wrote a new memory per
# call (1,723 on the janitor persona). It was in no AGENT_ID_MAP entry, so the
# request ledger filed it under CUSTOM_SYSTEM_JANITOR_AGENT — which is why a
# "janitor agent" appeared to run during every news scrape.
UNMAPPED_CALLERS = {
    "audit_worker", "equation_lab",
    "judge_evaluator", "memory_briefer", "memory_consolidator",
    "query_decomposer", "skillopt_optimizer", "strategy_evaluator",
}


def _literal_agent_names() -> dict[str, str]:
    """Every literal agent name passed at a call site in app/."""
    found: dict[str, str] = {}
    for f in APP.rglob("*.py"):
        if f.name == "prism_agent_registry.py":
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg in (
                "agent_name", "fallback_agent_name", "agent_id"
            ):
                v = node.value
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    found[v.value] = f"{f.relative_to(APP.parent)}:{node.lineno}"
    return found


class TestEveryCallerResolvesWhereWeThinkItDoes:
    def test_the_set_of_callers_has_not_drifted(self):
        """Set equality BOTH directions — a count can stay put while two names
        go missing and two are invented."""
        found = set(_literal_agent_names())

        assert found == set(EXPECTED_RESOLUTION), (
            f"new callers: {sorted(found - set(EXPECTED_RESOLUTION))}; "
            f"gone: {sorted(set(EXPECTED_RESOLUTION) - found)}"
        )

    def test_each_one_still_resolves_to_the_same_persona(self):
        drifted = {
            name: (expected, resolve_agent_id(name))
            for name, expected in EXPECTED_RESOLUTION.items()
            if resolve_agent_id(name) != expected
        }

        assert not drifted, f"persona drift (name: was -> now): {drifted}"


class TestTheSilentFallbackIsVisible:
    def test_the_unmapped_callers_are_exactly_the_known_ten(self):
        """An open item, pinned. Shrinking this set is the fix; growing it
        silently is the bug returning."""
        unmapped = {
            n for n in _literal_agent_names()
            if n and n not in AGENT_ID_MAP
            and resolve_agent_id(n) == "CUSTOM_SYSTEM_JANITOR_AGENT"
        }

        assert unmapped == UNMAPPED_CALLERS, (
            f"newly falling back: {sorted(unmapped - UNMAPPED_CALLERS)}; "
            f"now mapped: {sorted(UNMAPPED_CALLERS - unmapped)}"
        )

    def test_the_fallback_announces_itself(self):
        """It used to be silent, which is why it lasted."""
        import inspect

        from app.services import prism_agent_registry

        src = inspect.getsource(prism_agent_registry.resolve_agent_id)
        assert "_REPORTED_FALLBACKS" in src
