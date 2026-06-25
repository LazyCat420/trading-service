import sys

with open("scripts/canary_loop.py", "r") as f:
    content = f.read()

# Replace the deduplicated handling
old_code = """            if "status" in res[0] and res[0]["status"] == "deduplicated":
                print(f"Cycle deduplicated: {res[0]}")
                break"""

new_code = """            if "status" in res[0] and res[0]["status"] == "deduplicated":
                print(f"Cycle deduplicated: {res[0]}")
                # Fetch currently running cycle_id
                cur.execute("SELECT cycle_id FROM pipeline_state WHERE singleton_id = 'current' AND status = 'running';")
                running_row = cur.fetchone()
                if running_row and running_row[0]:
                    cycle_id = running_row[0]
                    print(f"Monitoring existing cycle: {cycle_id}")
                break"""

if old_code in content:
    content = content.replace(old_code, new_code)
    with open("scripts/canary_loop.py", "w") as f:
        f.write(content)
    print("Patched canary_loop.py")
else:
    print("Could not find code to replace")
