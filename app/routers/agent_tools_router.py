"""
agent_tools_router.py — Read-only endpoint for the tool registry.

Reads from the local tool_schemas.json file rather than querying
external services at runtime — avoids latency and dependency on
Rod's containers being up.
"""

import json
import logging
import os
from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-tools", tags=["agent-studio"])

_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "tool_schemas.json"
)
_SCHEMA_PATH = os.path.normpath(_SCHEMA_PATH)

# Cache the tool list in memory after first load
_tool_cache: list[dict] | None = None


def _load_tools() -> list[dict]:
    """Load and cache tool schemas from the local JSON file."""
    global _tool_cache
    if _tool_cache is not None:
        return _tool_cache

    if not os.path.exists(_SCHEMA_PATH):
        logger.warning(
            "[AgentTools] tool_schemas.json not found at %s — returning empty list",
            _SCHEMA_PATH,
        )
        _tool_cache = []
        return _tool_cache

    try:
        with open(_SCHEMA_PATH, "r") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("[AgentTools] Failed to parse tool_schemas.json: %s", e)
        _tool_cache = []
        return _tool_cache

    tools = []
    for schema in raw:
        tools.append({
            "id": schema.get("name", ""),
            "label": schema.get("name", "").replace("_", " ").title(),
            "description": schema.get("description", ""),
            "source": schema.get("source", "unknown"),
            "category": schema.get("category", "general"),
            "tier": schema.get("tier", 0),
            "permission": schema.get("permission", "read_only"),
        })

    _tool_cache = tools
    logger.info("[AgentTools] Loaded %d tools from tool_schemas.json", len(tools))
    return _tool_cache


@router.get("")
async def list_tools():
    """Return the full list of available tools for agent assignment."""
    tools = _load_tools()
    return {"tools": tools, "count": len(tools)}
