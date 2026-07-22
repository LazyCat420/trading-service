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
            from app.db import mongo_store
            if mongo_store.writes_mongo("agent_tool_telemetry"):
                mongo_store.insert_docs("agent_tool_telemetry", [_rec])
        except Exception:
            pass
    except Exception as e:
        logger.warning(
            "[ToolTelemetry] Failed to record %s/%s (non-fatal): %s",
            agent_name, tool_name, e,
        )
