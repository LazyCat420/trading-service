with open("app/services/prism_client.py", "r") as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if "enabledTools" in line or "get_agent_enabled_tool_names" in line:
        start = max(0, i-2)
        end = min(len(lines), i+8)
        print("---")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}", end="")
