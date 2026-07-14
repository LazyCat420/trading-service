"""
agent_tools_router.py — Read-only endpoint for the tool registry.

Reads from the local tool_schemas.json file rather than querying
external services at runtime — avoids latency and dependency on
Rod's containers being up.
"""

import json
import logging
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-tools", tags=["agent-studio"])

security = HTTPBearer()


def _verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != settings.API_SERVER_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Server Key")
    return credentials.credentials


class ToolUsagePayload(BaseModel):
    tool_name: str
    agent_name: Optional[str] = ""
    ticker: Optional[str] = ""
    cycle_id: Optional[str] = ""
    success: bool = True
    execution_ms: int = 0
    error_message: Optional[str] = None
    service_source: Optional[str] = "trading-service"


@router.post("/usage")
def report_tool_usage(
    payload: ToolUsagePayload,
    token: str = Depends(_verify_api_key)
):
    """Log a tool usage event from any service/source."""
    from app.services.logging.tool_logging import log_tool_call
    log_tool_call(
        tool_name=payload.tool_name,
        agent_name=payload.agent_name,
        ticker=payload.ticker,
        cycle_id=payload.cycle_id,
        success=payload.success,
        execution_ms=payload.execution_ms,
        error_message=payload.error_message,
        service_source=payload.service_source,
    )
    return {"status": "ok"}

class ToolExecutePayload(BaseModel):
    tool_name: str
    arguments: dict = {}
    agent_name: Optional[str] = ""
    ticker: Optional[str] = ""
    cycle_id: Optional[str] = ""


@router.post("/execute")
async def execute_tool(
    payload: ToolExecutePayload,
    token: str = Depends(_verify_api_key)
):
    """Execute a local-catalog tool and return its result.

    HTTP replacement for the scripts/execute_tool.py subprocess bridge —
    lazy-tool-service's container has no Python interpreter, so its
    LocalToolRouter calls this endpoint for python-bridge tools.
    """
    from app.tools.registry import registry

    tool_call = {
        "id": "call_lazy_tool_bridge",
        "type": "function",
        "function": {
            "name": payload.tool_name,
            "arguments": json.dumps(payload.arguments or {}),
        },
    }
    try:
        # force_local: this endpoint IS the execution target lazy-tool-service
        # delegates to — honoring USE_LAZY_TOOL_SERVICE here would bounce the
        # call straight back to lazy-tool-service in an infinite loop.
        result = await registry.execute_tool_call(
            tool_call,
            skip_permission_check=True,
            agent_name=payload.agent_name or "",
            ticker=payload.ticker or "",
            cycle_id=payload.cycle_id or "",
            force_local=True,
        )
        return result
    except Exception as e:
        logger.error("[AgentTools] execute %s failed: %s", payload.tool_name, e)
        raise HTTPException(status_code=500, detail=f"Tool execution failed: {e}")


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
            "name": schema.get("name", ""),
            "description": schema.get("description", ""),
            "domain": schema.get("domain", "Other"),
            "labels": schema.get("labels", []),
            "source": schema.get("source", "unknown"),
            "tier": schema.get("tier", 0),
            "permission": schema.get("permission", "read_only"),
            "tags": schema.get("tags", []),
        })

    _tool_cache = tools
    logger.info("[AgentTools] Loaded %d tools from tool_schemas.json", len(tools))
    return _tool_cache


@router.get("")
async def list_tools():
    """Return the full list of available tools for agent assignment."""
    tools = _load_tools()
    return {"tools": tools, "count": len(tools)}
