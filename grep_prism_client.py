import re
with open("app/services/prism_client.py", "r") as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if "def get_chat_payload_and_url" in line or "max_iterations" in line or "max_loops" in line:
        start = max(0, i-2)
        end = min(len(lines), i+8)
        print("---")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}", end="")
