"""
Agent Tool Telemetry — records per-tool-call metrics to Postgres.

Phase 3A: Each tool invocation (success, failure, or blocked) is recorded
to the `agent_tool_telemetry` table for debugging and performance analysis.

Usage:
    from app.v3.tool_telemetry import record_tool_call

    record_tool_call(
        cycle_id="cycle_abc123",
        agent_name="v3_junior_analyst",
        tool_name="get_market_data",
        args_hash="sha256...",
        success=True,
        elapsed_ms=450,
    )
"""

import hashlib
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _hash_args(arguments: dict | None) -> str:
    """Create a deterministic hash of tool arguments for dedup detection."""
    if not arguments:
        return "empty"
    try:
        canonical = json.dumps(arguments, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]
    except Exception:
        return "unhashable"


# Prism renames every tool it registers. Strip before comparing to a whitelist
# or every call looks non-compliant (this exact artifact produced a "zero
# whitelisted tools are used by any agent" misread on 2026-07-25).
_MCP_PREFIX = "mcp__lazy-tool-service__"

# Framework-injected; never on an agent whitelist by design.
_META_TOOLS = frozenset({
    "discover_and_enable_tools", "enable_tools", "search_tools", "think",
})

# Reaching any of these from a trading agent is a security regression, not
# drift. Each was observed SUCCEEDING before the lockdown.
_FORBIDDEN = frozenset({
    "execute_command", "execute_python", "execute_javascript", "execute_skill",
    "write_file", "query_datastore",
})


def _canary_check(agent_name: str, tool_name: str) -> None:
    """Log loudly when an agent calls something outside its whitelist.

    Deliberately does NOT block the call: this module is telemetry, and a
    telemetry path that can abort a cycle is worse than the drift it detects.
    The enforcement lives in the Prism persona pin; this makes a breach
    *visible* the moment it happens instead of at the next manual audit.
    """
    try:
        if not agent_name or not str(agent_name).startswith("v3_"):
            return
        tool = str(tool_name or "")
        if tool.startswith(_MCP_PREFIX):
            tool = tool[len(_MCP_PREFIX):]
        if not tool:
            # Empty tool names: 175 of these landed on 2026-07-13, all failures,
            # none since. Cheap to keep flagging — a silent malformed-dispatch
            # path is how that went unnoticed for weeks.
            logger.warning(
                "[ToolCanary] %s emitted an EMPTY tool name — malformed tool "
                "dispatch (last seen 2026-07-13)", agent_name,
            )
            return
        if tool in _META_TOOLS:
            return

        from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

        allowed = AGENT_TOOL_WHITELISTS.get(agent_name)
        if allowed is None or tool in allowed:
            return

        severity = logger.error if tool in _FORBIDDEN else logger.warning
        severity(
            "[ToolCanary] OFF-WHITELIST%s: %s called %r, which is not on its "
            "whitelist. The 2026-07-22 meta-tool lockdown should make this "
            "impossible — check the CUSTOM_V3_* persona availableTools pin.",
            " (FORBIDDEN)" if tool in _FORBIDDEN else "", agent_name, tool,
        )
    except Exception as e:  # never let the canary break telemetry
        logger.debug("[ToolCanary] check failed (non-fatal): %s", e)


def record_tool_call(
    cycle_id: str,
    agent_name: str,
    tool_name: str,
    args_hash: str = "",
    success: bool = True,
    elapsed_ms: int = 0,
    error_message: str = "",
    was_blocked: bool = False,
    ticker: str = "",
) -> None:
    """Record a single tool call to the agent_tool_telemetry table.

    Non-fatal: all exceptions are caught and logged. Tool telemetry
    should never abort a pipeline.
    """
    # ── Off-whitelist canary (2026-07-25) ──
    # The 2026-07-22 meta-tool lockdown (bad7904) closed a real hole: agents
    # had reached execute_command / write_file / execute_python through
    # catalog discovery, and the calls SUCCEEDED. Off-whitelist calls have
    # been zero since 07-23 — but that was protected by nothing, and the only
    # evidence of a regression would have been a tool name sitting in a table
    # nobody queries. A unit test catches config drift at CI time; this
    # catches a Prism-side persona re-sync at RUNTIME, which is the path the
    # original hole actually came through.
    _canary_check(agent_name, tool_name)

    try:
        from app.db.connection import get_db

        _rec = {
            "id": str(uuid.uuid4()), "cycle_id": cycle_id, "agent_name": agent_name,
            "tool_name": tool_name, "args_hash": args_hash or "", "success": success,
            "elapsed_ms": elapsed_ms, "error_message": error_message or "",
            "was_blocked": was_blocked, "ticker": ticker or "",
        }
        with get_db() as db:
            db.execute(
                """
                INSERT INTO agent_tool_telemetry
                    (id, cycle_id, agent_name, tool_name, args_hash,
                     success, elapsed_ms, error_message, was_blocked, ticker)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [_rec["id"], _rec["cycle_id"], _rec["agent_name"], _rec["tool_name"], _rec["args_hash"],
                 _rec["success"], _rec["elapsed_ms"], _rec["error_message"], _rec["was_blocked"], _rec["ticker"]],
            )
        try:
            from datetime import datetime, timezone
            from app.db import mongo_store
            if mongo_store.writes_mongo("agent_tool_telemetry"):
                # PG fills created_at via column default; the mirror must set it.
                mongo_store.insert_docs(
                    "agent_tool_telemetry",
                    [{**_rec, "created_at": datetime.now(timezone.utc)}],
                )
        except Exception as me:
            logger.warning("[ToolTelemetry] Mongo mirror failed (non-fatal): %s", me)
    except Exception as e:
        logger.warning(
            "[ToolTelemetry] Failed to record %s/%s (non-fatal): %s",
            agent_name, tool_name, e,
        )
