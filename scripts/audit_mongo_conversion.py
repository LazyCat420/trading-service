"""Audit MongoDB vs PostgreSQL Conversion Status Across Trading Service & Trading Client."""

import json
import os
import re
from pathlib import Path

TS_ROOT = Path("/home/lazycat/github/projects/sun/.worktrees/ts-quality-purge")
TC_ROOT = Path("/home/lazycat/github/projects/sun/.worktrees/tc-mongo-conversion")
LAS_ROOT = Path("/home/lazycat/github/projects/sun/.worktrees/las-remove-postgres")
SDK_ROOT = Path("/home/lazycat/github/projects/sun/.worktrees/sdk-test-no-postgres")

def analyze_collection_map():
    map_file = TS_ROOT / "app/db/collection_map.json"
    with open(map_file) as f:
        data = json.load(f)
    collections = data.get("collections", {})
    return collections

def count_pg_references(root_dir: Path, extensions=(".py", ".ts", ".js")):
    pg_patterns = [
        re.compile(r"\bget_db\(\)"),
        re.compile(r"\bget_connection\(\)"),
        re.compile(r"\bpsycopg2?\b"),
        re.compile(r"\basyncpg\b"),
        re.compile(r"\bsqlalchemy\b"),
    ]
    matches = {}
    for p in root_dir.rglob("*"):
        if any(part in p.parts for part in ("node_modules", ".venv", ".git", "__pycache__", "tests", "scratch")):
            continue
        if p.suffix in extensions and p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                file_matches = []
                for pat in pg_patterns:
                    found = pat.findall(content)
                    if found:
                        file_matches.extend(found)
                if file_matches:
                    matches[str(p.relative_to(root_dir))] = file_matches
            except Exception:
                pass
    return matches

def main():
    collections = analyze_collection_map()
    total_collections = len(collections)
    
    print("=" * 60)
    print(f"TRADING PIPELINE MONGODB CONVERSION AUDIT")
    print("=" * 60)
    print(f"Total Collections Mapped in collection_map.json: {total_collections}")
    
    prefixes = {}
    for table, meta in collections.items():
        coll = meta.get("collection", "")
        prefix = coll.split("_")[0] if "_" in coll else "other"
        prefixes[prefix] = prefixes.get(prefix, 0) + 1
    
    for prefix, count in sorted(prefixes.items()):
        print(f"  - Prefix `{prefix}_`: {count} collections")
        
    print("\n--- Scanning Codebases for Active PG Drivers / Calls ---")
    ts_pg = count_pg_references(TS_ROOT / "app")
    tc_pg = count_pg_references(TC_ROOT / "app")
    las_pg = count_pg_references(LAS_ROOT / "src")
    sdk_pg = count_pg_references(SDK_ROOT / "lazycat_sdk")
    
    print(f"trading-service (app/): {len(ts_pg)} files with legacy PG refs")
    for f, m in ts_pg.items():
        print(f"    {f}: {set(m)}")
    print(f"trading-client (app/): {len(tc_pg)} files with legacy PG refs")
    for f, m in tc_pg.items():
        print(f"    {f}: {set(m)}")
    print(f"lazy-agent-service (src/): {len(las_pg)} files with legacy PG refs")
    print(f"lazycat-sdk (lazycat_sdk/): {len(sdk_pg)} files with legacy PG refs")
    print("=" * 60)

if __name__ == "__main__":
    main()
