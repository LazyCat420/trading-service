import json
import logging
from app.tools.registry import registry, PermissionLevel
from app.tools.tool_context import current_agent_name, current_cycle_id
from app.agents.whiteboard import whiteboard

logger = logging.getLogger(__name__)

@registry.register(
    name="request_peer_analysis",
    description="Request a specific peer agent (e.g. 'quant_analyst', 'fundamental_analyst', 'junior_analyst') to run a query, check a lead, or verify details, and post findings to the whiteboard.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker to research."
            },
            "target_agent": {
                "type": "string",
                "description": "The exact name of the target agent (junior_analyst, fundamental_analyst, quant_analyst)."
            },
            "query": {
                "type": "string",
                "description": "The specific question or instructions for the target agent."
            }
        },
        "required": ["ticker", "target_agent", "query"]
    },
    tier=1,
    source="agent_coordination",
    permission=PermissionLevel.WRITE,
)
async def request_peer_analysis(ticker: str, target_agent: str, query: str) -> str:
    cycle_id = current_cycle_id()
    author_agent = current_agent_name()
    logger.info("[AgentTools] request_peer_analysis called for %s to %s (query=%s)", ticker, target_agent, query)
    
    ticker = ticker.upper().strip()
    target_agent = target_agent.lower().strip()
    
    try:
        # Load existing tasks from the task_queue section
        section_data = await whiteboard.get_section(ticker=ticker, cycle_id=cycle_id, section="task_queue")
        tasks = []
        if section_data and isinstance(section_data.get("content"), dict):
            tasks = section_data["content"].get("tasks", [])
        
        # Dedup: agents (and the proxy's tool-result cache misses) re-file the
        # same request within a cycle — an identical pending/running task means
        # the work is already queued, so acknowledge instead of double-queuing.
        for t in tasks:
            if (
                isinstance(t, dict)
                and t.get("target_agent") == target_agent
                and (t.get("query") or "").strip() == query.strip()
                and t.get("status") in ("pending", "running")
            ):
                return json.dumps({
                    "status": "success",
                    "message": f"Identical task for {target_agent} is already {t.get('status')} — not re-queued.",
                })

        # Append the new task
        new_task = {
            "target_agent": target_agent,
            "query": query,
            "requested_by": author_agent,
            "status": "pending"
        }
        tasks.append(new_task)
        
        # Write back to whiteboard
        await whiteboard.write_section(
            ticker=ticker,
            cycle_id=cycle_id,
            section="task_queue",
            content={"tasks": tasks},
            author_agent=author_agent
        )
        return json.dumps({"status": "success", "message": f"Task queued for {target_agent}."})
    except Exception as e:
        logger.error("[AgentTools] request_peer_analysis failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="escalate_to_pm",
    description="Immediately escalate the cycle to the Board of Directors and Portfolio Manager, bypass standard research loops, and submit the reasoning/findings.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker."
            },
            "reason": {
                "type": "string",
                "description": "The critical reason or justification for escalation."
            }
        },
        "required": ["ticker", "reason"]
    },
    tier=1,
    source="agent_coordination",
    permission=PermissionLevel.WRITE,
)
async def escalate_to_pm(ticker: str, reason: str) -> str:
    cycle_id = current_cycle_id()
    author_agent = current_agent_name()
    logger.info("[AgentTools] escalate_to_pm called for %s by %s (reason=%s)", ticker, author_agent, reason)
    
    ticker = ticker.upper().strip()
    
    try:
        # Set escalation flag in whiteboard section 'escalation'
        await whiteboard.write_section(
            ticker=ticker,
            cycle_id=cycle_id,
            section="escalation",
            content={"escalated": True, "reason": reason, "by": author_agent},
            author_agent=author_agent
        )
        return json.dumps({"status": "success", "message": "Escalation registered."})
    except Exception as e:
        logger.error("[AgentTools] escalate_to_pm failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
