import re
with open("app/agents/base_agent.py", "r") as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if "run_prism_agent" in line or "tools_override" in line:
        start = max(0, i-5)
        end = min(len(lines), i+6)
        print("---")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}", end="")
