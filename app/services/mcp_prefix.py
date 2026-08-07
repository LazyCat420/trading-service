"""The MCP namespace prism wraps our tools in — one definition, both spellings.

**Why this module exists.** The prefix was hardcoded in at least four places:
`app/services/logging/tool_logging.py`, `app/services/tool_optimizer.py` (whose
comment literally read "Must stay in sync with tool_logging.py"), and
`app/v3/tool_telemetry.py` on the strip side, plus `app/agents/base_agent.py`
and `app/v3/prism_registration.py` on the construct side. Five copies of one
string is how a rename half-lands: the constructors keep minting a name the
strippers no longer recognise, and the damage is silent — a namespaced name
that fails to strip does not raise, it just fails to match a whitelist, so it
reads as "the agent called an off-whitelist tool". That artifact already
produced one false "zero whitelisted tools are used by any agent" read on
2026-07-25.

**Both spellings are accepted, one is emitted.** The service behind these tools
was renamed `lazy-tool-service` -> `lazy-agent-service` on 2026-08-07. The
prefix is minted by PRISM from its MCP server registration name, not by us.

**The cut-over completed on 2026-08-07 evening.** The server now registers as
`lazy-agent-service` in all three scopes, the `lazy-tool-service` rows were
deleted, and prism serves `mcp__lazy-tool-service__*` nowhere. Stripping still
accepts every spelling and must keep doing so — telemetry, whiteboard rows and
playbook entries recorded under the old namespace long outlive the
registration.

`MCP_EMIT_PREFIX` is what we construct with, and **its default is now the live
name.** It was left defaulting to the old spelling during the cut-over, which
turned out to be a trap: `scripts/sync_prism_v3_personas.py` computes desired
tool names through this module, the env var exists only inside the deployed
container, so running that script from a dev machine proposed reverting all 13
personas to a prefix prism no longer serves. A dry run caught it. A default
that is only correct in one environment is a defect, not a configuration.
"""

from __future__ import annotations

import os

#: Emitted when we build a namespaced tool name. The default MUST match what
#: prism actually serves — a script run outside the container gets this value,
#: and the wrong one silently scopes an agent to tools that do not exist.
MCP_EMIT_PREFIX = os.getenv("MCP_EMIT_PREFIX", "mcp__lazy-agent-service__")

#: Every namespace a live call may arrive under. Order matters only in that a
#: longer prefix must precede any prefix of itself; these share no such
#: relationship. `mcp_` is the catch-all trailer and MUST stay last.
MCP_PREFIXES: tuple[str, ...] = (
    "mcp__lazy-agent-service__",
    "mcp__lazy-tool-service__",
    "mcp__lazy-tools__",
    "mcp_",
)


def strip_mcp_prefix(name: str | None) -> str:
    """Namespaced tool name -> canonical bare name.

    Bare names pass through untouched, so this is safe to call on anything.
    """
    out = (name or "").strip()
    for prefix in MCP_PREFIXES:
        if out.startswith(prefix):
            return out[len(prefix):]
    return out


def mcp_tool_name(bare_name: str) -> str:
    """Bare name -> the namespaced form we advertise.

    Already-namespaced names and `domain:` selectors pass through, so callers
    can hand this a mixed whitelist without pre-filtering.
    """
    name = (bare_name or "").strip()
    if not name or name.startswith("mcp__") or name.startswith("domain:"):
        return name
    return f"{MCP_EMIT_PREFIX}{name}"
