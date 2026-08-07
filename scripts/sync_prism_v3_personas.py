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

sys.path.insert(0, ".")
# THIS SCRIPT IS THE SECOND WRITER OF availableTools. `app/v3/prism_registration.py`
# writes the same prism field at boot and mints its prefix from
# `MCP_EMIT_PREFIX`. This file used to hardcode `mcp__lazy-tool-service__`, so
# running it after the registration flip would have silently rewritten all
# eleven personas to a prefix prism no longer routes — every v3 agent losing
# every tool, with the script cheerfully printing "UPDATE" for each one. Two
# writers of one field must read the prefix from one place.
from app.services.mcp_prefix import mcp_tool_name  # noqa: E402

NONE_SENTINEL = "__no_tools__"

def _persona_sources() -> dict[str, list[str]]:
    """prism persona agentId -> the agent modules whose whitelists scope it.

    DISCOVERED, not listed. This was a hand-maintained dict of eleven personas
    and it had drifted by two: `v3_bull_defense` and `v3_valuation_analyst` are
    real agent modules with real whitelists, registered on prism by
    `prism_registration._discover_v3_agent_modules()`, and this script simply
    never saw them. Measured live 2026-08-07 — both carried
    `enabledByDefaultTools: []` against a non-empty `availableTools`, which is
    the maximum-headroom state that makes prism re-attach the discovery
    meta-tools. Valuation had seven tools available and none enabled: scoped on
    paper, tool-less in practice, and holding the door open for the trio.

    That is the same failure `_V3_AGENT_MODULES` had, in the same package, for
    the same reason. A list you must remember to append to fails silently: the
    agent still runs, just unscoped. Deriving the id the way the registrar does
    (`CUSTOM_{AGENT_NAME.upper()}`) makes adding a module sufficient.

    One persona per agent: a shared persona would need the UNION of whitelists
    as availableTools, and any agent enabling only ITS OWN subset then has
    permanent headroom — observed live 2026-07-22 when delta shared junior's.
    """
    import importlib
    import pkgutil

    import app.v3.agents as pkg

    sources: dict[str, list[str]] = {}
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        try:
            module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        except Exception as e:  # noqa: BLE001 — one bad module must not stop the rest
            print(f"WARN     cannot import app.v3.agents.{mod_info.name}: {e}")
            continue
        agent_name = getattr(module, "AGENT_NAME", None)
        if not agent_name or getattr(module, "TOOL_WHITELIST", None) is None:
            continue
        sources[f"CUSTOM_{agent_name.upper()}"] = [agent_name]
    return sources


def _whitelists() -> dict[str, list[str]]:
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
    return AGENT_TOOL_WHITELISTS


def _mcp_names(tools: list[str]) -> list[str]:
    if not tools:
        return [NONE_SENTINEL]
    return sorted(mcp_tool_name(t) for t in dict.fromkeys(tools))


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
    persona_sources = _persona_sources()
    print(f"Discovered {len(persona_sources)} V3 agent modules to scope: "
          f"{', '.join(sorted(persona_sources))}\n")
    existing = _request(f"{args.prism}/custom-agents")
    if isinstance(existing, dict):
        existing = existing.get("agents") or existing.get("data") or []
    by_id = {a.get("agentId"): a for a in existing}

    changed = skipped = missing = 0
    for persona_id, sources in sorted(persona_sources.items()):
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
