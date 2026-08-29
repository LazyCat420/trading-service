import json
import logging
from app.tools.registry import registry, PermissionLevel
from app.tools.tool_context import current_agent_name, current_cycle_id
from app.agents.whiteboard import whiteboard

logger = logging.getLogger(__name__)

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
