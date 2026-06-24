with open("app/services/prism_client.py", "r") as f:
    lines = f.readlines()
for i in range(462, 510):
    if i < len(lines):
        print(f"{i+1}: {lines[i]}", end="")
