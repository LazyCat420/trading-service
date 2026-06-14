import os
import sys

with open("plans/debate.md") as f:
    lines = f.readlines()
    for i, line in enumerate(lines):
        if "verdict" in line.lower() or "swarm" in line.lower():
            print(f"L{i}: {line.strip()}")
