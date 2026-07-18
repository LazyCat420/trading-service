import json
import urllib.request
import subprocess
import os
import sys

# 1. Get native trading-service schemas using the existing export script
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
export_script = os.path.join(current_dir, "export_tool_schemas.py")

result = subprocess.run([sys.executable, export_script], capture_output=True, text=True, check=True)
native_schemas = json.loads(result.stdout)

# 2. Fetch prism-service aggregated tools (which includes tools-service)
PRISM_URL = "http://10.0.0.16:7777/config/tools"
# Prism attributes requests by the x-project / x-username HEADERS (it ignores
# the same fields in a JSON body); without them the call is filed under prism's
# catch-all "default"/"anonymous" project.
_prism_request = urllib.request.Request(
    PRISM_URL,
    headers={
        "x-project": os.getenv("PRISM_PROJECT", "vllm-trading-bot"),
        "x-username": os.getenv("PRISM_USERNAME", "lazy-trader"),
    },
)
try:
    req = urllib.request.urlopen(_prism_request, timeout=10)
    prism_schemas = json.loads(req.read().decode("utf-8"))
except Exception as e:
    print(f"Warning: Failed to fetch prism-service schemas: {e}")
    prism_schemas = []

# ── Owner classification ─────────────────────────────────────────────
# The registry is shared across apps: HTML-Notes reads this same file as its
# live tool schema, and treesearch strain tools arrive via the prism merge.
# Every tool is stamped with its owning app so consumers (e.g. the
# trading-client Tools tab) can separate them instead of showing one mixed list.
FOREIGN_PREFIXES = {"strain_": "treesearch", "html_notes_": "html-notes", "canvas_": "html-notes"}
FOREIGN_DOMAINS = {"Cannabis Research": "treesearch", "HTML Notes": "html-notes"}
FOREIGN_SOURCES = {"treesearch": "treesearch", "html-notes": "html-notes", "canvas_manager": "html-notes"}


def classify_owner(schema):
    name = schema.get("name", "")
    for prefix, app in FOREIGN_PREFIXES.items():
        if name.startswith(prefix):
            return app
    if schema.get("domain") in FOREIGN_DOMAINS:
        return FOREIGN_DOMAINS[schema["domain"]]
    if schema.get("source") in FOREIGN_SOURCES:
        return FOREIGN_SOURCES[schema["source"]]
    if "canvas" in (schema.get("tags") or []):
        return "html-notes"
    return "trading"


# 3. Load whitelists. This must not fail open: a silent `allowed_tools = None`
# used to disable filtering entirely and let every prism tool into the registry.
sys.path.insert(0, project_root)
try:
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
    from app.tools import registry
except Exception as e:
    sys.exit(f"FATAL: cannot load tool whitelists/registry ({e}); refusing to generate an unfiltered registry.")

allowed_tools = set()
for tools_list in AGENT_TOOL_WHITELISTS.values():
    allowed_tools.update(tools_list)
# Also keep all native tools defined in trading-service
real_native_names = {name for name, func in registry.tools.items() if func is not None}
allowed_tools.update(real_native_names)
print(f"Loaded whitelist filtering. {len(allowed_tools)} unique tools allowed (including {len(real_native_names)} native python tools).")

merged_schemas = []
seen_names = set()


def admit(schema):
    """Keep trading tools only if whitelisted/native; keep foreign-app tools
    deliberately (HTML-Notes and treesearch consume this registry too)."""
    name = schema.get("name")
    if not name or name in seen_names:
        return
    if name in allowed_tools or classify_owner(schema) != "trading":
        merged_schemas.append(schema)
        seen_names.add(name)


for schema in native_schemas:
    admit(schema)

for schema in prism_schemas:
    name = schema.get("name")
    if name:
        if name.startswith("mcp__lazy-tool-service__"):
            schema["name"] = name.replace("mcp__lazy-tool-service__", "")
        elif name.startswith("mcp__"):
            # Skip tools from other MCP servers
            continue
        admit(schema)

# 4. Stamp ownership metadata on every tool.
for schema in merged_schemas:
    schema["owner_app"] = classify_owner(schema)
    schema["agents"] = sorted(
        agent for agent, tools_list in AGENT_TOOL_WHITELISTS.items()
        if schema.get("name") in tools_list
    )

# 5. Write the per-domain source folder, then build the flat artifacts.
# The split folder (lazy-tool-service/tool_schemas/) is the source of truth;
# the flat tool_schemas.json copies are build outputs kept in sync across repos.
sys.path.insert(0, current_dir)
from build_tool_schemas import build, write_split

written = write_split(merged_schemas)
for rel, count in written.items():
    print(f"  {rel}: {count} tools")
build()
print(f"Successfully generated {len(merged_schemas)} tools into the split source + flat artifacts.")
