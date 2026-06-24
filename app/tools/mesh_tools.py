"""
Mesh Tools for lateral agent-to-agent fact-checking and communications in the Swarm Mesh.
"""

import asyncio
import logging
import uuid
import json
from pydantic import BaseModel, Field
from app.tools.registry import registry, PermissionLevel
# 
class DummyEventBus:
    def subscribe(self, *args, **kwargs): pass
    def publish(self, *args, **kwargs): pass
    def unsubscribe(self, *args, **kwargs): pass
event_bus = DummyEventBus()

logger = logging.getLogger(__name__)

class FactCheckSchema(BaseModel):
    query: str = Field(..., description="The specific question, fact, or metric to verify (e.g., Q1 2026 gross margins).")
    target_agent: str = Field("retriever", description="The role of the agent who should answer this query. Can be 'retriever' or 'technical_analyst'.")

@registry.register(
    name="request_lateral_fact_check",
    description=(
        "Ask another agent in the mesh (e.g., retriever or technical_analyst) to dynamically verify "
        "a fact, fetch specific evidence, or check indicator patterns during a debate round. "
        "This tool executes asynchronously over the event bus."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The specific question, fact, or metric to verify."
            },
            "target_agent": {
                "type": "string",
                "enum": ["retriever", "technical_analyst"],
                "description": "The role of the target agent who should answer this query (default is 'retriever')."
            }
        },
        "required": ["query"]
    },
    permission=PermissionLevel.READ_ONLY,
    input_model=FactCheckSchema,
)
async def request_lateral_fact_check(query: str, target_agent: str = "retriever", **kwargs) -> str:
    """Request a fact check from another agent in the mesh asynchronously."""
    ticker = kwargs.get("ticker")
    cycle_id = kwargs.get("cycle_id")
    
    if not ticker or not cycle_id:
        return "Error: Missing ticker or cycle_id context for lateral fact check."
        
    correlation_id = str(uuid.uuid4()).split("-")[0]
    reply_channel = f"FACT_CHECK_RESOLVED_{correlation_id}"
    future = asyncio.get_running_loop().create_future()
    
    async def response_handler(payload: dict):
        if payload.get("correlation_id") == correlation_id:
            future.set_result(payload.get("evidence", "No evidence returned."))
            
    event_bus.subscribe(reply_channel, response_handler)
    
    logger.info(f"[MeshTool] Publishing FACT_CHECK_REQUESTED: correlation_id={correlation_id}, target={target_agent}, query='{query}'")
    
    event_bus.publish("FACT_CHECK_REQUESTED", {
        "correlation_id": correlation_id,
        "ticker": ticker,
        "query": query,
        "cycle_id": cycle_id,
        "target_agent": target_agent,
        "reply_channel": reply_channel
    })
    
    try:
        # 90 second safety timeout
        result = await asyncio.wait_for(future, timeout=90.0)
        return f"Verified Evidence from {target_agent}:\n{result}"
    except asyncio.TimeoutError:
        logger.warning(f"[MeshTool] Fact-check request timed out: correlation_id={correlation_id}, query='{query}'")
        return f"Error: Fact-check request to {target_agent} timed out. Proceed with caution."
    finally:
        event_bus.unsubscribe(reply_channel, response_handler)
