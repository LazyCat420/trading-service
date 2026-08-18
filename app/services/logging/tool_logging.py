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
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

from app.services.mcp_prefix import strip_mcp_prefix
from app.db import mongo_query, mongo_store

#: `service_source` values that are NOT production traffic. Anything logged
#: under one of these is a synthetic call — an audit probe, a contract test, a
#: reachability sweep — and must be excluded from every question of the form
#: "is this tool used?" or "is this tool healthy?".
#:
#: This exists because the 2026-07-15 ecosystem audit curled its way through the
#: catalog and every probe landed in `tool_usage_stats` indistinguishable from a
#: real call. It is still there: each of `strain_detail`, `html_notes_web_search`,
#: `canvas_read_dom` and a dozen others shows 1-2 lifetime "calls", all stamped
#: 2026-07-15, all from that sweep. Read naively, a tool nobody uses looks used.
#:
#: The sharper hazard is the other direction. `tool_optimizer` computes tool
#: reputation from this table and PRUNES low-success tools out of live agents'
#: schemas. A probe suite deliberately sends malformed payloads to check that a
#: tool rejects them predictably — so an untagged probe run scores a working
#: tool as failing and takes it away from the agents that depend on it. Probing
#: is only safe once the probe rows are separable.
PROBE_SERVICE_SOURCES: tuple[str, ...] = ("audit-probe", "contract-test")

#: Ready-to-interpolate SQL guard. Callers that measure production behaviour
#: should AND this into their WHERE clause.
PROBE_EXCLUSION_SQL = (
    "COALESCE(service_source, '') <> ALL(ARRAY["
    + ", ".join(f"'{s}'" for s in PROBE_SERVICE_SOURCES)
    + "])"
)


def _normalize_tool_name(raw_name: str) -> str:
    """Strip MCP transport prefixes to produce a canonical tool name.

    The prefix list moved to `app.services.mcp_prefix` — it was duplicated here
    and in `tool_optimizer.py`, which carried a "must stay in sync" comment
    instead of an import.
    """
    return strip_mcp_prefix(raw_name)


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
        # Deduplicate: skip if the same tool+agent+cycle was logged within the
        # last 5 seconds. This prevents double-counting when both
        # lazy-tool-service and prism_agent_harness log the same call.
        if cycle_id:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=5)
            if mongo_query.exists('tool_usage_stats', {
                'tool_name': canonical_name,
                'agent_name': agent_name or "",
                'cycle_id': cycle_id,
                'called_at': {'$gt': cutoff},
            }):
                logger.debug(
                    "[ToolLogger] Skipping duplicate log for '%s' (agent=%s, cycle=%s)",
                    canonical_name, agent_name, cycle_id,
                )
                return

        now_utc = datetime.now(timezone.utc)
        mongo_store.insert_docs('tool_usage_stats', [{'tool_name': canonical_name, 'agent_name': agent_name or "", 'ticker': ticker or "", 'cycle_id': cycle_id or "", 'success': success, 'execution_ms': execution_ms, 'error_message': error_message, 'service_source': service_source, 'called_at': now_utc}])
    except Exception as e:
        logger.debug("[ToolLogger] Failed to log tool execution for '%s': %s", canonical_name, e)
