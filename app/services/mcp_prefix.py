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
prefix is minted by PRISM from its MCP server registration name, not by us, so
which one arrives depends on which registration is connected — and right now
BOTH are: `lazy-agent-service` in the `coding/admin` scope, `lazy-tool-service`
in `vllm-trading-bot/lazy-trader` and `html-notes-client/admin`. Stripping must
therefore handle either, and it must keep doing so until every scope has moved.

`MCP_EMIT_PREFIX` is what we construct with. It is env-overridable precisely so
the cut-over does not need a code change in this repo: set
`MCP_EMIT_PREFIX=mcp__lazy-agent-service__` once prism serves that name in the
trading scope.
"""

from __future__ import annotations

import os

#: Emitted when we build a namespaced tool name. Env-overridable so the
#: registration flip is a config change, not a deploy of five files.
MCP_EMIT_PREFIX = os.getenv("MCP_EMIT_PREFIX", "mcp__lazy-tool-service__")

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
