import re
with open("app/tools/prism_agent_harness.py", "r") as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if "active_tools =" in line or "def run_prism_agent" in line or "tools_override" in line:
        start = max(0, i-2)
        end = min(len(lines), i+3)
        print("---")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}", end="")
