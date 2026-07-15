"""
Dynamic Tool Discovery — System prompt fragments for teaching agents
about Prism's dynamic tool discovery and activation system.

When agents are routed through Prism's /agent endpoint, they gain access
to meta-tools that allow them to discover and enable additional tools
mid-loop. This module provides the prompt guidance that teaches agents
how and when to use these capabilities.
"""

# ── Prism Meta-Tool Names ──────────────────────────────────────────────
# These are Prism-local tools (NOT MCP-prefixed) that allow agents to
# dynamically modify their tool set during an agentic loop, and spawn subagents.
PRISM_DYNAMIC_META_TOOLS = [
    "discover_and_enable_tools",
    "enable_tools",
    "disable_tools",
    "search_tools",
    "create_subagent",
    "create_subagents",
    "send_subagent_message",
    "stop_subagent",
]

# The DYNAMIC_TOOL_DISCOVERY_PROMPT system-prompt fragment that used to live
# here had zero consumers (confirmed in 82aa2b4) and was removed. The
# meta-tool names above are still appended to enabledTools for non-v3 agents
# by tool_whitelists.get_agent_enabled_tool_names.
