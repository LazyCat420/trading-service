"""Every v3 persona must register DENY policies for the forbidden tools.

`availableTools` is not enforcement. Prism's registerCustom() drops
`coreToolsLocked` when it converts our Mongo document into a persona, so
AgenticToolResolver's `persona?.coreToolsLocked ?? true` defaults a custom
agent to LOCKED and force-adds the entire CORE_AGENTIC / system set on top of
the whitelist we pinned. That is how the v3 analysts reached execute_command,
write_file and query_datastore — 12 calls between 2026-07-18 and 2026-08-03,
all but one successful — while carrying a whitelist naming none of them.

A DENY policy is the one restriction that survives: AutoApprovalEngine
evaluates policies before the tier system AND before full-auto, and treats DENY
as terminal. These agents register with auto_approve=True, so nothing else can
stop a call.

If this file fails, agents can run shell commands and write files again.
"""
from __future__ import annotations

import inspect

import pytest

from app.v3.prism_registration import (
    _V3_DENIED_TOOLS,
    _V3_TOOL_POLICIES,
    register_v3_agents,
)


def test_the_denylist_covers_code_execution_and_file_writes():
    for tool in ("execute_command", "execute_javascript", "execute_skill",
                 "write_file", "query_datastore"):
        assert tool in _V3_DENIED_TOOLS, (
            f"{tool} was observed SUCCEEDING from a v3 trading agent; "
            f"removing it from the denylist re-opens that hole"
        )


def test_execute_python_is_allowed_deliberately():
    """A reviewed exception (2026-08-03), not an oversight.

    tools-service runs it as a subprocess with socket creation blocked,
    RLIMIT_DATA capped and a temp cwd that is wiped — it does not execute in
    the trading container. If this ever needs reversing, the reasoning is in
    app/v3/prism_registration.py next to _V3_DENIED_TOOLS.
    """
    assert "execute_python" not in _V3_DENIED_TOOLS


def test_every_policy_is_a_terminal_deny():
    assert _V3_TOOL_POLICIES, "an empty policy list registers no enforcement at all"
    for policy in _V3_TOOL_POLICIES:
        assert policy["decision"] == "DENY", (
            f"{policy['tool']}: ASK_USER is answered 'yes' by full-auto "
            f"(AutoApprovalEngine.check) — only DENY is terminal"
        )
        # Prism matches tool names exactly; a prefixed or wildcard entry would
        # silently never fire.
        assert policy["tool"] in _V3_DENIED_TOOLS
        assert not policy["tool"].startswith("mcp__"), (
            "core tools are not MCP-prefixed; a prefixed rule never matches"
        )


def test_the_serialized_shape_is_what_prism_reconstructs():
    """registerCustom() reads exactly {tool, decision, name}."""
    for policy in _V3_TOOL_POLICIES:
        assert set(policy) == {"tool", "decision", "name"}
        assert all(isinstance(v, str) for v in policy.values())


def test_registration_actually_passes_the_policies():
    """The denylist existing is worthless if it is never sent.

    Guards the failure mode this whole module exists to prevent: config that
    looks like enforcement but is wired to nothing.
    """
    source = inspect.getsource(register_v3_agents)

    assert source.count("policies=_V3_TOOL_POLICIES") >= 2, (
        "both the V3 agent loop and the core-agent loop must pass policies= ; "
        "an agent registered without them keeps full core-tool access"
    )


@pytest.mark.parametrize("tool", sorted(_V3_DENIED_TOOLS))
def test_no_v3_agent_whitelists_a_denied_tool(tool):
    """Belt and braces: the denylist and the whitelists must not disagree."""
    import importlib
    import pkgutil

    import app.v3.agents as pkg

    for mod_info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        whitelist = getattr(module, "TOOL_WHITELIST", None) or []
        assert tool not in whitelist, (
            f"{getattr(module, 'AGENT_NAME', mod_info.name)} whitelists {tool} "
            f"while the policy DENIES it — the agent would be handed a tool "
            f"every call of which is then rejected"
        )
