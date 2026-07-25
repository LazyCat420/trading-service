"""The meta-tool lockdown must not silently regress.

## What this protects

Before commit `bad7904` (2026-07-22, "meta-tool lockdown"), V3 agents reached
tools nobody granted them — via `discover_and_enable_tools`, which let them
browse Prism's full catalog. Measured from `agent_tool_telemetry`, live agents
successfully called:

    execute_command      1 call,   1 success
    write_file           2 calls,  2 successes
    execute_javascript   4 calls,  4 successes
    execute_python      11 calls, 11 successes
    get_stock          122 calls, 114 successes   (not even in our registry)

Arbitrary code execution and filesystem writes, succeeding, in the agent that
decides trades. The fix pinned each `CUSTOM_V3_*` persona's `availableTools`
to the code whitelist so Prism sees zero discovery headroom.

**Since 2026-07-23 the off-whitelist count is ZERO.** That is the single most
valuable safety property in the tool layer, and until this file existed it was
protected by nothing at all — one persona re-sync writing an empty
`availableTools` would have silently restored full-catalog access, and the only
evidence would have been a tool name in a telemetry table nobody queries.

## Why the empty-list case gets its own test

An EMPTY `availableTools` does not mean "no tools" on the Prism side — it means
UNSCOPED, i.e. full-catalog discovery headroom. That is why
`decision_agent.py` carries a single `whiteboard_read` sentinel and why
`portfolio_manager.py` keeps a whitelist despite running with
`enable_tools=False`. Deleting either as "dead config" reintroduces the exact
hole this file guards. Observed live on `CUSTOM_V3_DECISION_SYNTHESIZER`,
2026-07-22.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

# Tools the framework injects; never on an agent whitelist by design.
META_TOOLS = {"discover_and_enable_tools", "enable_tools", "search_tools", "think"}

# If any of these is ever reachable by a V3 agent, that is a security
# regression, not a config drift. Each was observed SUCCEEDING pre-lockdown.
FORBIDDEN_FOR_V3 = {
    "execute_command",
    "execute_python",
    "execute_javascript",
    "execute_skill",
    "write_file",
    "read_url",
    "query_datastore",
}


def _v3_agent_modules():
    import app.v3.agents as pkg

    for mod_info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        name = getattr(module, "AGENT_NAME", None)
        wl = getattr(module, "TOOL_WHITELIST", None)
        if name and wl is not None:
            yield name, module, list(wl)


def test_every_v3_agent_declares_a_whitelist():
    """A missing TOOL_WHITELIST is indistinguishable from an empty one by the
    time it reaches Prism — and empty means UNSCOPED."""
    agents = dict((n, wl) for n, _m, wl in _v3_agent_modules())
    assert agents, "no V3 agent modules found — the loader is broken, not the config"
    for name, wl in agents.items():
        assert isinstance(wl, list), f"{name}: TOOL_WHITELIST must be a list"


def test_no_whitelist_is_empty():
    """THE regression guard. Empty availableTools == unscoped full-catalog
    access in Prism. An agent that genuinely needs no tools carries a sentinel
    (see decision_agent.py), it does NOT carry an empty list."""
    for name, _module, wl in _v3_agent_modules():
        assert len(wl) > 0, (
            f"{name} has an EMPTY TOOL_WHITELIST. Empty does not mean 'no tools' — "
            f"Prism reads it as UNSCOPED and grants full-catalog discovery. "
            f"Use a read-only sentinel (e.g. ['whiteboard_read']) instead."
        )


def test_no_v3_agent_can_reach_code_execution_or_file_writes():
    """Pre-lockdown, agents reached execute_command/write_file/execute_python
    through discovery and the calls SUCCEEDED. Nothing may grant them now."""
    for name, _module, wl in _v3_agent_modules():
        offenders = sorted(set(wl) & FORBIDDEN_FOR_V3)
        assert not offenders, (
            f"{name} whitelists {offenders} — code execution / file writes must "
            f"never be reachable from a trading agent (regression of bad7904)"
        )


def test_meta_tools_are_never_whitelisted():
    """Meta-tools are the discovery path itself. Granting one explicitly would
    re-open the catalog the lockdown closed."""
    for name, _module, wl in _v3_agent_modules():
        offenders = sorted(set(wl) & META_TOOLS)
        assert not offenders, (
            f"{name} whitelists meta-tool(s) {offenders} — these grant catalog "
            f"discovery and must not appear on an agent whitelist"
        )


def test_portfolio_manager_keeps_a_nonempty_whitelist():
    """The PM runs with enable_tools=False (pipeline_service.py), so its
    whitelist looks like dead config and has been proposed for deletion twice.
    It is NOT dead: prism_registration reads module.TOOL_WHITELIST directly and
    an empty list registers an UNSCOPED agent."""
    from app.v3.agents import portfolio_manager

    assert getattr(portfolio_manager, "TOOL_WHITELIST", None), (
        "portfolio_manager.TOOL_WHITELIST must stay non-empty even though the "
        "PM runs with enable_tools=False — empty means unscoped in Prism"
    )


def test_merged_dict_matches_the_modules():
    """`app/agents/tool_whitelists.py` MERGES the V3 modules at import; it is
    not a competing copy. The two used to be hand-maintained and disagreed for
    7 of 9 agents. If this fails, the merge broke — fix the merge, do not
    hand-edit the dict."""
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    for name, _module, wl in _v3_agent_modules():
        assert name in AGENT_TOOL_WHITELISTS, (
            f"{name} did not reach AGENT_TOOL_WHITELISTS — "
            f"_merge_v3_module_whitelists() is broken"
        )
        assert AGENT_TOOL_WHITELISTS[name] == wl, (
            f"{name}: merged dict disagrees with the module. The module is the "
            f"source of truth; the dict must derive from it."
        )


def test_no_prompt_names_a_tool_the_agent_cannot_call():
    """Pruning a tool whose SYSTEM_PROMPT still instructs its use turns a live
    instruction into a dead end.

    This caught the 2026-07-25 prune mid-flight: `schedule_research`,
    `request_research_now`, `get_reddit_trending_stocks`,
    `get_technical_indicators` and `get_finnhub_news` all showed ZERO calls in
    60 days and looked safe to delete — but each is named in its agent's prompt
    as a conditional fallback ("A value genuinely absent → fetch it"). Zero
    calls meant the primary path was healthy, not that the tool was dead.
    If a tool is genuinely unwanted, remove the PROMPT line first.
    """
    import re

    for name, module, wl in _v3_agent_modules():
        prompt = getattr(module, "SYSTEM_PROMPT", "") or ""
        named = set(re.findall(r"`([a-z_]{4,})`", prompt))
        named |= set(re.findall(r"([a-z_]{6,})\(", prompt))
        candidates = {
            t for t in named
            if t.startswith(("get_", "calculate_", "run_", "schedule_", "list_",
                             "request_", "save_", "forecast_", "watch_"))
        }
        orphaned = sorted(candidates - set(wl))
        assert not orphaned, (
            f"{name}: SYSTEM_PROMPT instructs {orphaned} but they are not on "
            f"TOOL_WHITELIST. Delete the prompt instruction before the tool."
        )


@pytest.mark.parametrize("agent_name", ["v3_decision_synthesizer"])
def test_no_tool_agents_use_a_sentinel_not_an_empty_list(agent_name):
    """decision_synthesizer is 'pure reasoning, no tools' by design and runs
    296x/30d with zero tool calls. It still must not carry an empty list."""
    wl = dict((n, w) for n, _m, w in _v3_agent_modules())[agent_name]
    assert len(wl) >= 1
    assert not (set(wl) & (FORBIDDEN_FOR_V3 | META_TOOLS))
