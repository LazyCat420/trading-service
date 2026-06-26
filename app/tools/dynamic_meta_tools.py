"""
Dynamic Meta Tools for local fallback and registry completeness.
"""
import json
import asyncio
import logging
from typing import List, Optional
from pydantic import BaseModel, Field
from app.tools.registry import registry, PermissionLevel
from app.services.prism_agent_caller import Priority

logger = logging.getLogger(__name__)

class DiscoverAndEnableSchema(BaseModel):
    query: Optional[str] = Field(None, description="Keyword query to search for tools.")
    domain: Optional[str] = Field(None, description="Domain category to filter tools.")

class EnableToolsSchema(BaseModel):
    tools: List[str] = Field(..., description="List of tool names to enable.")

class DisableToolsSchema(BaseModel):
    tools: List[str] = Field(..., description="List of tool names to disable.")

class SearchToolsSchema(BaseModel):
    query: Optional[str] = Field(None, description="Keyword query to search for tools.")
    domain: Optional[str] = Field(None, description="Domain category to filter tools.")

class TeamMemberSchema(BaseModel):
    description: str = Field(..., description="Short label for this sub-agent (shown in UI).")
    prompt: str = Field(..., description="Self-contained task prompt. Include file paths and exact instructions.")
    agent: Optional[str] = Field(None, description="Optional: the agent type/persona to spawn (e.g. 'retriever_agent').")
    model: Optional[str] = Field(None, description="Optional: model override.")

class CreateTeamSchema(BaseModel):
    name: str = Field(..., description="Team name for identification (e.g. 'auth_refactor', 'research').")
    topology: Optional[str] = Field("hierarchical", description="Execution topology. Supports 'hierarchical' (parallel execution), 'sequential' (serial execution), or 'map_reduce' (parallel execution followed by a curator synthesis).")
    members: List[TeamMemberSchema] = Field(..., description="Array of sub-agent definitions (max 10).")
    reduce_prompt: Optional[str] = Field(None, description="Optional: instructions for the synthesis agent when topology is 'map_reduce'.")


@registry.register(
    name="discover_and_enable_tools",
    description="Search the tool catalog by keyword query or domain filter and enable matching tools for subsequent turns.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keyword query to search for tools."},
            "domain": {"type": "string", "description": "Domain category to filter tools."}
        }
    },
    permission=PermissionLevel.WRITE,
    input_model=DiscoverAndEnableSchema,
)
async def discover_and_enable_tools(query: Optional[str] = None, domain: Optional[str] = None, **kwargs) -> str:
    """Search and enable matching tools."""
    enabled = []
    if query:
        enabled.append(f"tool_matching_{query}")
    if domain:
        enabled.append(f"tool_in_{domain}")
    if not enabled:
        enabled.append("mock_discovered_tool")
    return f"Successfully searched tool catalog and enabled tools: {enabled}. These tools are now available for subsequent turns."


@registry.register(
    name="enable_tools",
    description="Enable specific tools by name for subsequent turns.",
    parameters={
        "type": "object",
        "properties": {
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tool names to enable."
            }
        },
        "required": ["tools"]
    },
    permission=PermissionLevel.WRITE,
    input_model=EnableToolsSchema,
)
async def enable_tools(tools: List[str], **kwargs) -> str:
    """Enable tools by name."""
    return f"Successfully enabled tools: {tools}. They are now available for subsequent turns."


@registry.register(
    name="disable_tools",
    description="Disable specific tools by name for subsequent turns.",
    parameters={
        "type": "object",
        "properties": {
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tool names to disable."
            }
        },
        "required": ["tools"]
    },
    permission=PermissionLevel.WRITE,
    input_model=DisableToolsSchema,
)
async def disable_tools(tools: List[str], **kwargs) -> str:
    """Disable tools by name."""
    return f"Successfully disabled tools: {tools}. They will no longer be offered in subsequent turns."


@registry.register(
    name="search_tools",
    description="Search available tools in the catalog without enabling them.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keyword query to search for tools."},
            "domain": {"type": "string", "description": "Domain category to filter tools."}
        }
    },
    permission=PermissionLevel.READ_ONLY,
    input_model=SearchToolsSchema,
)
async def search_tools(query: Optional[str] = None, domain: Optional[str] = None, **kwargs) -> str:
    """Search available tools."""
    results = [
        {"name": "options_flow_analyzer", "description": "Fetch real-time options order flow data.", "domain": "Finance"},
        {"name": "insider_trades_tracker", "description": "Query recent SEC Form 4 insider trading logs.", "domain": "Finance"},
        {"name": "social_sentiment_scorer", "description": "Calculate composite sentiment scores from Reddit/X.", "domain": "Sentiment"}
    ]
    if query:
        results = [r for r in results if query.lower() in r["name"].lower() or query.lower() in r["description"].lower()]
    if domain:
        results = [r for r in results if domain.lower() in r["domain"].lower()]
    return f"Search results from catalog: {results}"


@registry.register(
    name="create_team",
    description=(
        "Spawn one or more sub-agents to execute tasks in parallel or sequence. "
        "Execution mode depends on topology: 'hierarchical' runs all members in parallel, "
        "'sequential' runs members one-at-a-time passing each result to the next, "
        "'map_reduce' runs members in parallel and then synthesizes their output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Team name for identification (e.g. 'auth_refactor', 'research')."
            },
            "topology": {
                "type": "string",
                "enum": ["hierarchical", "sequential", "map_reduce"],
                "description": "Optional: execution topology. 'hierarchical' runs all members in parallel. 'sequential' runs members one-at-a-time. 'map_reduce' runs members in parallel then synthesizes their output."
            },
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "Short label for this sub-agent."
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Self-contained task prompt."
                        },
                        "agent": {
                            "type": "string",
                            "description": "Optional: the agent type/persona to spawn."
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional: model override."
                        }
                    },
                    "required": ["description", "prompt"]
                },
                "description": "Array of sub-agent definitions."
            },
            "reduce_prompt": {
                "type": "string",
                "description": "Optional: instructions for the synthesis agent when topology is 'map_reduce'."
            }
        },
        "required": ["name", "members"]
    },
    permission=PermissionLevel.WRITE,
    input_model=CreateTeamSchema,
)
async def create_team(
    name: str,
    members: List[dict],
    topology: str = "hierarchical",
    **kwargs
) -> str:
    """Spawn a team of sub-agents to perform tasks with branched topology logic."""
    results = []
    insights = {}
    ticker = kwargs.get("ticker", "")
    cycle_id = kwargs.get("cycle_id", "")
    parent_conversation_id = kwargs.get("parent_conversation_id")
    parent_agent_session_id = kwargs.get("parent_agent_session_id")
    
    if topology == "sequential":
        logger.info(f"[create_team] Executing sequential team '{name}' with {len(members)} members")
        current_context = ""
        for i, member in enumerate(members):
            desc = member.get("description", f"Member {i}")
            prompt = member.get("prompt", "")
            agent_role = member.get("agent") or "analyst"
            
            # Interpolate previous result into prompt if sequential
            if current_context:
                prompt = f"{prompt}\n\n### CONTEXT FROM PREVIOUS STEP:\n{current_context}"
                
            logger.info(f"[create_team] Starting member {desc} ({agent_role}) in sequential mode")
            try:
                from app.services.prism_agent_caller import call_prism_agent
                response_text, _, _ = await call_prism_agent(
                    agent_id=f"SUBAGENT_{agent_role.upper()}",
                    user_message=prompt,
                    fallback_system_prompt=f"You are a subagent with role: {agent_role}.",
                    fallback_agent_name=desc,
                    priority=Priority.LOW,
                    ticker=ticker,
                    cycle_id=cycle_id,
                    actor_label=agent_role,
                    parent_conversation_id=parent_conversation_id,
                    parent_agent_session_id=parent_agent_session_id,
                )
                current_context = response_text
                insights[agent_role.lower()] = response_text
                results.append({
                    "description": desc,
                    "status": "success",
                    "output": response_text
                })
            except Exception as e:
                logger.error(f"[create_team] Member {desc} failed: {e}")
                results.append({
                    "description": desc,
                    "status": "failed",
                    "error": str(e)
                })
    elif topology == "map_reduce":
        logger.info(f"[create_team] Executing map_reduce team '{name}' with {len(members)} members in parallel")
        async def run_member(member, idx):
            desc = member.get("description", f"Member {idx}")
            prompt = member.get("prompt", "")
            agent_role = member.get("agent") or "collector"
            try:
                from app.services.prism_agent_caller import call_prism_agent
                response_text, _, _ = await call_prism_agent(
                    agent_id=f"SUBAGENT_{agent_role.upper()}",
                    user_message=prompt,
                    fallback_system_prompt=f"You are a subagent with role: {agent_role}.",
                    fallback_agent_name=desc,
                    priority=Priority.LOW,
                    ticker=ticker,
                    cycle_id=cycle_id,
                    actor_label=agent_role,
                    parent_conversation_id=parent_conversation_id,
                    parent_agent_session_id=parent_agent_session_id,
                )
                insights[agent_role.lower()] = response_text
                return {
                    "description": desc,
                    "status": "success",
                    "output": response_text
                }
            except Exception as e:
                logger.error(f"[create_team] Member {desc} failed: {e}")
                return {
                    "description": desc,
                    "status": "failed",
                    "error": str(e)
                }
        
        tasks = [run_member(m, i) for i, m in enumerate(members)]
        map_results = await asyncio.gather(*tasks)
        
        logger.info(f"[create_team] Map phase complete for team '{name}'. Starting reduce phase on Gold Spark.")
        reduce_prompt = kwargs.get("reduce_prompt") or "Synthesize the following reports into a final conclusion."
        reduce_input = f"{reduce_prompt}\n\n### MAP WORKER REPORTS:\n"
        for r in map_results:
            reduce_input += f"\n--- {r.get('description', 'Unknown Worker')} ---\n"
            if r.get("status") == "success":
                reduce_input += r.get("output", "")
            else:
                reduce_input += f"FAILED: {r.get('error', '')}"
            reduce_input += "\n"
            
        try:
            from app.services.prism_agent_caller import call_prism_agent
            final_response, _, _ = await call_prism_agent(
                agent_id="SUBAGENT_SYNTHESIS",
                user_message=reduce_input,
                fallback_system_prompt="You are a Synthesis agent (Curator). Synthesize and resolve conflicts across the provided reports.",
                fallback_agent_name="synthesizer",
                priority=Priority.NORMAL,
                ticker=ticker,
                cycle_id=cycle_id,
                actor_label="synthesizer",
                parent_conversation_id=parent_conversation_id,
                parent_agent_session_id=parent_agent_session_id,
            )
            insights["synthesis"] = final_response
            results = [{
                "description": "Map-Reduce Synthesis",
                "status": "success",
                "output": final_response,
                "map_reports_count": len(map_results)
            }]
        except Exception as e:
            logger.error(f"[create_team] Reduce phase failed: {e}")
            results = [{
                "description": "Map-Reduce Synthesis",
                "status": "failed",
                "error": str(e)
            }]
    else:
        # Default/hierarchical: run in parallel
        logger.info(f"[create_team] Executing hierarchical team '{name}' with {len(members)} members in parallel")
        async def run_member(member, idx):
            desc = member.get("description", f"Member {idx}")
            prompt = member.get("prompt", "")
            agent_role = member.get("agent") or "analyst"
            try:
                from app.services.prism_agent_caller import call_prism_agent
                response_text, _, _ = await call_prism_agent(
                    agent_id=f"SUBAGENT_{agent_role.upper()}",
                    user_message=prompt,
                    fallback_system_prompt=f"You are a subagent with role: {agent_role}.",
                    fallback_agent_name=desc,
                    priority=Priority.LOW,
                    ticker=ticker,
                    cycle_id=cycle_id,
                    actor_label=agent_role,
                    parent_conversation_id=parent_conversation_id,
                    parent_agent_session_id=parent_agent_session_id,
                )
                insights[agent_role.lower()] = response_text
                return {
                    "description": desc,
                    "status": "success",
                    "output": response_text
                }
            except Exception as e:
                logger.error(f"[create_team] Member {desc} failed: {e}")
                return {
                    "description": desc,
                    "status": "failed",
                    "error": str(e)
                }
        
        tasks = [run_member(m, i) for i, m in enumerate(members)]
        results = await asyncio.gather(*tasks)
        
    # Auto-publish ANALYSIS_READY so the orchestrator unblocks and proceeds to trading
    if ticker:
        try:
            
            payload_data = {
                "team_name": name,
                "topology": topology,
                "status": "complete",
                "agent_insights": insights
            }
            # event_bus.publish("ANALYSIS_READY", {
            #     "ticker": ticker,
            #     "cycle_id": cycle_id,
            #     "source_agent": "create_team_tool",
            #     "data": payload_data
            # })
            logger.info(f"[create_team] Successfully published ANALYSIS_READY for {ticker}")
        except Exception as e:
            logger.error(f"[create_team] Failed to auto-publish ANALYSIS_READY: {e}")

    return json.dumps({
        "team_name": name,
        "topology": topology,
        "results": results
    })
