"""
Tool Logging Service — Tracks tool usage counts, latencies, success/failure status, and errors.

All tool names are normalized before storage:
  - MCP prefixes (e.g. 'mcp__lazy-tool-service__get_market_data') are stripped
    so the DB always stores canonical names ('get_market_data').
  - Duplicate calls (same tool+agent+cycle within a short window) are
    deduplicated to prevent double-counting from parallel write paths
    (e.g. lazy-tool-service reportUsage + prism_agent_harness log_tool_call).
"""

import logging
from datetime import datetime, timezone
from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Known MCP prefixes that should be stripped for canonical names
_MCP_PREFIXES = (
    "mcp__lazy-tool-service__",
    "mcp__lazy-tools__",
    "mcp_",
)


def _normalize_tool_name(raw_name: str) -> str:
    """Strip MCP transport prefixes to produce a canonical tool name."""
    name = raw_name
    for prefix in _MCP_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name


def log_tool_call(
    tool_name: str,
    agent_name: str = "",
    ticker: str = "",
    cycle_id: str = "",
    success: bool = True,
    execution_ms: int = 0,
    error_message: str | None = None,
    service_source: str = "trading-service"
):
    """
    Log a tool execution into the database.
    Fire-and-forget, suppresses all database connection issues to preserve tool reliability.
    """
    # Normalize the tool name so all execution paths produce consistent DB entries
    canonical_name = _normalize_tool_name(tool_name)

    try:
        with get_db() as db:
            # Deduplicate: skip if the same tool+agent+cycle was logged within the
            # last 5 seconds. This prevents double-counting when both
            # lazy-tool-service and prism_agent_harness log the same call.
            if cycle_id:
                dup = db.execute(
                    """
                    SELECT 1 FROM tool_usage_stats
                    WHERE tool_name = %s
                      AND agent_name = %s
                      AND cycle_id = %s
                      AND called_at > NOW() - INTERVAL '5 seconds'
                    LIMIT 1
                    """,
                    (canonical_name, agent_name or "", cycle_id),
                ).fetchone()
                if dup:
                    logger.debug(
                        "[ToolLogger] Skipping duplicate log for '%s' (agent=%s, cycle=%s)",
                        canonical_name, agent_name, cycle_id,
                    )
                    return

            db.execute(
                """
                INSERT INTO tool_usage_stats 
                (tool_name, agent_name, ticker, cycle_id, success, execution_ms, error_message, service_source, called_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    canonical_name,
                    agent_name or "",
                    ticker or "",
                    cycle_id or "",
                    success,
                    execution_ms,
                    error_message,
                    service_source,
                    datetime.now(timezone.utc)
                )
            )
    except Exception as e:
        logger.debug("[ToolLogger] Failed to log tool execution for '%s': %s", canonical_name, e)
