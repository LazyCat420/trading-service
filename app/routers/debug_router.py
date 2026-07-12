"""
Debug Router — Real-time cycle debugging API endpoints.

Extends the existing diagnostics_router with health probes,
conversation transcripts, and agent tool call traces.

All endpoints are read-only and require no user input validation
beyond the cycle_id and ticker path parameters (used as-is for
dict lookups, not SQL queries).
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/health")
async def run_health_check():
    """Run all service health probes on demand."""
    from app.services.logging.service_health_probe import run_all_probes
    results = await run_all_probes()
    healthy = sum(1 for r in results if r["status"] == "healthy")
    return {
        "status": "ok" if healthy == len(results) else "degraded",
        "healthy": healthy,
        "total": len(results),
        "probes": results,
    }


@router.get("/transcript/{cycle_id}/{ticker}")
async def get_conversation_transcript(cycle_id: str, ticker: str):
    """Get the inter-agent conversation transcript for a ticker."""
    from app.services.logging.conversation_tracer import conversation_tracer
    transcript = conversation_tracer.get_transcript(cycle_id, ticker.upper())
    if not transcript:
        return {
            "cycle_id": cycle_id,
            "ticker": ticker.upper(),
            "turns": [],
            "message": "No conversation recorded yet.",
        }
    stats = conversation_tracer.get_stats(cycle_id, ticker.upper())
    return {
        "cycle_id": cycle_id,
        "ticker": ticker.upper(),
        "stats": stats,
        "turns": transcript,
    }


@router.get("/transcript/{cycle_id}/{ticker}/readable")
async def get_readable_transcript(cycle_id: str, ticker: str):
    """Get a human-readable conversation transcript."""
    from app.services.logging.conversation_tracer import conversation_tracer
    text = conversation_tracer.get_readable_transcript(cycle_id, ticker.upper())
    return {"transcript": text}


@router.get("/services")
async def get_service_status():
    """Quick service connectivity overview."""
    from app.services.logging.service_health_probe import run_all_probes
    results = await run_all_probes()
    return {
        "services": {
            r["service"]: {
                "status": r["status"],
                "latency_ms": r["latency_ms"],
                "error": r.get("error"),
            }
            for r in results
        }
    }


@router.get("/tools")
async def list_registered_tools():
    """List all registered tools with their metadata."""
    from app.tools.registry import registry
    tools = []
    for name, func in registry.tools.items():
        meta = registry.get_tool_meta(name)
        tools.append({
            "name": name,
            "tier": meta.tier if meta else "unknown",
            "source": meta.source if meta else "unknown",
            "tags": meta.tags if meta else [],
        })
    return {"total": len(tools), "tools": tools}


@router.get("/tools/agents")
async def list_agent_tool_access():
    """Show which tools each agent has access to."""
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
    result = {}
    for agent, tools in AGENT_TOOL_WHITELISTS.items():
        coord_tools = [
            t for t in tools
            if t in ("post_finding", "read_team_findings",
                     "request_investigation", "check_open_investigations")
        ]
        result[agent] = {
            "total_tools": len(tools),
            "has_coordination": len(coord_tools) > 0,
            "coordination_tools": coord_tools,
            "all_tools": tools,
        }
    return {"agents": result}


