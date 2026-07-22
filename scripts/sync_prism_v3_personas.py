#!/usr/bin/env python3
"""Sync CUSTOM_V3_* prism personas' tool scope to the code whitelists.

Why (2026-07-21 research audit, finding F2): prism attaches its discovery
meta-tools (search_tools / enable_tools / discover_and_enable_tools) to any
agent with "discovery headroom" — persona availableTools not currently
enabled. Through that door, live v3 pipeline agents reached execute_command,
write_file, execute_python etc., bypassing the static whitelists entirely.

Fix (data-side; prism-service code is read-only ground truth): pin each
CUSTOM_V3_* persona's availableTools AND enabledByDefaultTools to exactly the
agent module's TOOL_WHITELIST in MCP naming. A scoped persona whose whole
universe is already enabled has zero headroom → prism drops the discovery
trio → nothing outside the whitelist is reachable.

Tool-less agents (decision synthesizer) get a sentinel name so the persona
counts as scoped with an empty discoverable universe — an EMPTY list would
mean UNSCOPED (full-catalog headroom), the exact bug this fixes.

Idempotent; personas that don't exist on prism are reported and skipped
(creating one needs identity fields that belong to a human decision).

Usage: python3 scripts/sync_prism_v3_personas.py [--prism http://10.0.0.16:7777] [--dry-run]
"""

import argparse
import json
import sys
import urllib.request

MCP_PREFIX = "mcp__lazy-tool-service__"
NONE_SENTINEL = "__no_tools__"

# agent module name -> prism persona agentId. One persona per agent: a shared
# persona would need the UNION of whitelists as availableTools, and any agent
# whose request enables only ITS OWN subset then has permanent discovery
# headroom — prism re-attaches the meta-tools (observed live 2026-07-22 when
# delta shared the junior persona).
PERSONA_SOURCES: dict[str, list[str]] = {
    "CUSTOM_V3_JUNIOR_ANALYST": ["v3_junior_analyst"],
    "CUSTOM_V3_DELTA_ANALYST": ["v3_delta_analyst"],
    "CUSTOM_V3_FUNDAMENTAL_ANALYST": ["v3_fundamental_analyst"],
    "CUSTOM_V3_QUANT_ANALYST": ["v3_quant_analyst"],
    "CUSTOM_V3_REGIME_ENGINE": ["v3_regime_engine"],
    "CUSTOM_V3_BOARD_OF_DIRECTORS": ["v3_board_of_directors"],
    "CUSTOM_V3_DECISION_SYNTHESIZER": ["v3_decision_synthesizer"],
    "CUSTOM_V3_BULL_AGENT": ["v3_bull_agent"],
    "CUSTOM_V3_BEAR_AGENT": ["v3_bear_agent"],
    "CUSTOM_V3_DEBATE_JUDGE": ["v3_debate_judge"],
    "CUSTOM_V3_PORTFOLIO_MANAGER": ["v3_portfolio_manager"],
}


def _whitelists() -> dict[str, list[str]]:
    sys.path.insert(0, ".")
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
    return AGENT_TOOL_WHITELISTS


def _mcp_names(tools: list[str]) -> list[str]:
    if not tools:
        return [NONE_SENTINEL]
    return sorted(
        t if t.startswith("mcp__") else f"{MCP_PREFIX}{t}"
        for t in dict.fromkeys(tools)
    )


def _request(url: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prism", default="http://10.0.0.16:7777")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    whitelists = _whitelists()
    existing = _request(f"{args.prism}/custom-agents")
    if isinstance(existing, dict):
        existing = existing.get("agents") or existing.get("data") or []
    by_id = {a.get("agentId"): a for a in existing}

    changed = skipped = missing = 0
    for persona_id, sources in PERSONA_SOURCES.items():
        doc = by_id.get(persona_id)
        if not doc:
            print(f"MISSING  {persona_id} — not registered on prism, skipped")
            missing += 1
            continue

        tools: list[str] = []
        for src in sources:
            tools.extend(whitelists.get(src, []))
        target = _mcp_names(tools)

        current_avail = sorted(doc.get("availableTools") or [])
        current_default = sorted(doc.get("enabledByDefaultTools") or [])
        if current_avail == target and current_default == target:
            print(f"OK       {persona_id} ({len(target)} tools, already in sync)")
            skipped += 1
            continue

        print(f"UPDATE   {persona_id}: {len(current_avail)} available / "
              f"{len(current_default)} default → {len(target)} pinned")
        for extra in sorted(set(current_avail) - set(target)):
            print(f"           - removing {extra}")
        for added in sorted(set(target) - set(current_avail)):
            print(f"           + adding   {added}")

        if not args.dry_run:
            # PUT /custom-agents/:id expects the Mongo _id, not the agentId.
            mongo_id = doc.get("_id")
            if isinstance(mongo_id, dict):  # extended JSON {"$oid": "..."}
                mongo_id = mongo_id.get("$oid")
            if not mongo_id:
                print(f"           ! no _id on {persona_id}, cannot update")
                continue
            _request(
                f"{args.prism}/custom-agents/{mongo_id}",
                method="PUT",
                body={"availableTools": target, "enabledByDefaultTools": target},
            )
        changed += 1

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}"
          f"{changed} updated, {skipped} in sync, {missing} missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
