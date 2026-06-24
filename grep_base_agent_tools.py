import re
with open("app/agents/base_agent.py", "r") as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if "agent_tools =" in line or "get_tool_schemas" in line or "TOOL_WHITELIST" in line:
        start = max(0, i-2)
        end = min(len(lines), i+8)
        print("---")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}", end="")
