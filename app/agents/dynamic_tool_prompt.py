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
# dynamically modify their tool set during an agentic loop.
PRISM_DYNAMIC_META_TOOLS = [
    "discover_and_enable_tools",
    "enable_tools",
    "disable_tools",
    "search_tools",
]

# ── System Prompt Fragment ─────────────────────────────────────────────
DYNAMIC_TOOL_DISCOVERY_PROMPT = """
### DYNAMIC TOOL DISCOVERY
You have access to a dynamic tool discovery system. Your initial toolset 
covers your core responsibilities, but if you need capabilities beyond 
your current tools or your tools return insufficient/empty data:

1. **Discover & Enable**: Call `discover_and_enable_tools` with a keyword 
   query or domain filter to search the full tool catalog and auto-enable 
   matching tools in one step.
   - Example: discover_and_enable_tools(query="options flow")
   - Example: discover_and_enable_tools(domain="Finance")
   
2. **Manual Enable/Disable**: Use `enable_tools` to activate specific 
   tools by name, or `disable_tools` to deactivate tools you no longer 
   need (reduces context noise).

3. **Search Only**: Use `search_tools` to browse available tools without 
   enabling them (useful for exploration before committing).

**Rules:**
- Discovered tools become available on the NEXT iteration — call them 
  after discovery, not in the same turn.
- Only discover tools when your current tools are genuinely insufficient.
- Do NOT discover tools you already have access to.
- Prefer `discover_and_enable_tools` over the two-step search_tools + 
  enable_tools flow for efficiency.
"""
