"""Build the flat tool_schemas.json artifacts from the per-domain source folder.

Source of truth: lazy-tool-service/tool_schemas/<owner_app>/<domain>.json
(bare JSON arrays of tool entries). The build validates, merges them
deterministically, and writes the flat bare-array tool_schemas.json that every
runtime loader expects (Node gateway readers use process.cwd()/tool_schemas.json,
the Python SDK registry and routers read the repo-root copy) to all repos that
carry a copy: lazy-tool-service, trading-service, trading-client. Keeping the
copies byte-identical is asserted by trading-service/tests/test_multi_repo_audit.py.

Usage:
    python3 build_tool_schemas.py                # build flat files from the split source
    python3 build_tool_schemas.py --init <flat>  # one-time: explode a flat file into the source folder
"""

import json
import os
import re
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# This script lives in <sun>/trading-service/scripts (and is deploy-mirrored to
# lazy-tool-service/python/scripts, one level deeper), so walk up until the
# sibling repos are visible.
def _find_sun_root() -> str:
    d = _SCRIPT_DIR
    for _ in range(6):
        d = os.path.dirname(d)
        if os.path.isdir(os.path.join(d, "lazy-tool-service")):
            return d
    sys.exit("FATAL: cannot locate the sun repo root (no lazy-tool-service sibling found).")


SUN_ROOT = _find_sun_root()
SOURCE_DIR = os.path.join(SUN_ROOT, "lazy-tool-service", "tool_schemas")
FLAT_TARGETS = [
    os.path.join(SUN_ROOT, "lazy-tool-service", "tool_schemas.json"),
    os.path.join(SUN_ROOT, "trading-service", "tool_schemas.json"),
    os.path.join(SUN_ROOT, "trading-client", "tool_schemas.json"),
]

REQUIRED_KEYS = ("name", "description", "parameters")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "general").lower()).strip("-") or "general"


def write_split(tools: list, source_dir: str = SOURCE_DIR) -> dict:
    """Partition a flat tool list into <owner_app>/<domain>.json source files.

    Rewrites the whole tree so removed tools don't linger in stale files.
    """
    buckets = {}
    for t in tools:
        owner = t.get("owner_app") or "trading"
        key = (owner, _slug(t.get("domain")))
        buckets.setdefault(key, []).append(t)

    if os.path.isdir(source_dir):
        for root, _dirs, files in os.walk(source_dir):
            for f in files:
                if f.endswith(".json"):
                    os.remove(os.path.join(root, f))

    written = {}
    for (owner, domain), entries in sorted(buckets.items()):
        folder = os.path.join(source_dir, owner)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{domain}.json")
        entries.sort(key=lambda t: t["name"])
        with open(path, "w") as f:
            json.dump(entries, f, indent=2)
            f.write("\n")
        written[f"{owner}/{domain}.json"] = len(entries)
    return written


def load_split(source_dir: str = SOURCE_DIR) -> list:
    """Read every source file, validate, and return the merged flat list."""
    if not os.path.isdir(source_dir):
        sys.exit(f"FATAL: split source folder not found: {source_dir}")

    tools, seen = [], {}
    for root, _dirs, files in sorted(os.walk(source_dir)):
        for fname in sorted(files):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, source_dir)
            entries = json.load(open(path))
            if not isinstance(entries, list):
                sys.exit(f"FATAL: {rel} must be a bare JSON array of tool entries.")
            owner = os.path.basename(root)
            for t in entries:
                missing = [k for k in REQUIRED_KEYS if k not in t]
                if missing:
                    sys.exit(f"FATAL: {rel}: tool {t.get('name', '?')!r} missing {missing}.")
                if t["name"] in seen:
                    sys.exit(f"FATAL: duplicate tool {t['name']!r} in {rel} and {seen[t['name']]}.")
                seen[t["name"]] = rel
                t.setdefault("owner_app", owner)
                tools.append(t)
    tools.sort(key=lambda t: (t.get("owner_app", ""), t["name"]))
    return tools


def build(source_dir: str = SOURCE_DIR) -> list:
    """Merge the split sources and write every flat tool_schemas.json copy."""
    tools = load_split(source_dir)
    payload = json.dumps(tools, indent=2) + "\n"
    for target in FLAT_TARGETS:
        if not os.path.isdir(os.path.dirname(target)):
            print(f"skip (repo not present): {target}")
            continue
        with open(target, "w") as f:
            f.write(payload)
        print(f"wrote {len(tools)} tools -> {target}")
    return tools


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        flat = json.load(open(sys.argv[2]))
        written = write_split(flat)
        for rel, n in written.items():
            print(f"{rel}: {n} tools")
        build()
    else:
        build()
