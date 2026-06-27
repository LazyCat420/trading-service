import json
import urllib.request
import subprocess
import os

# 1. Get native trading-service schemas using the existing export script
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
export_script = os.path.join(current_dir, "export_tool_schemas.py")

import sys

result = subprocess.run([sys.executable, export_script], capture_output=True, text=True, check=True)
native_schemas = json.loads(result.stdout)

# 2. Fetch prism-service aggregated tools (which includes tools-service)
PRISM_URL = "http://10.0.0.16:7777/config/tools"
try:
    req = urllib.request.urlopen(PRISM_URL, timeout=10)
    prism_schemas = json.loads(req.read().decode("utf-8"))
except Exception as e:
    print(f"Warning: Failed to fetch prism-service schemas: {e}")
    prism_schemas = []

# Merge them (avoiding duplicates by name)
merged_schemas = []
seen_names = set()

for schema in native_schemas:
    name = schema.get("name")
    if name and name not in seen_names:
        merged_schemas.append(schema)
        seen_names.add(name)

for schema in prism_schemas:
    name = schema.get("name")
    if name:
        if name.startswith("mcp__lazy-tool-service__"):
            name = name.replace("mcp__lazy-tool-service__", "")
            schema["name"] = name
        elif name.startswith("mcp__"):
            # Skip tools from other MCP servers
            continue
        
        if name not in seen_names:
            merged_schemas.append(schema)
            seen_names.add(name)

# Write to tool_schemas.json in the project root
out_file = os.path.join(project_root, "tool_schemas.json")
with open(out_file, "w") as f:
    json.dump(merged_schemas, f, indent=2)

print(f"Successfully generated {out_file} with {len(merged_schemas)} tools.")
