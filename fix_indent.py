with open("app/collectors/congress_collector.py", "r") as f:
    lines = f.readlines()

for i in range(146, 194):  # lines 147 to 194 (0-indexed 146 to 193)
    if lines[i].startswith("    "):
        lines[i] = lines[i][4:]

with open("app/collectors/congress_collector.py", "w") as f:
    f.writelines(lines)
