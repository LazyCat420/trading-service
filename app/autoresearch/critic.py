import json
import logging
from typing import Dict, Any, List
from app.services.prism_agent_caller import PrismAgentCaller

logger = logging.getLogger(__name__)

async def evaluate_process(traces: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Evaluates process quality: logic soundness, sourcing, and tool usage accuracy.
    Uses the PRISM custom agent caller.
    """
    if not traces:
        return {"process_score": 0.0, "logic_score": 0.0, "tool_score": 0.0, "feedback": "No traces provided."}

    # Format traces for the LLM
    trace_summary = "\n".join([
        f"Step {i+1}: Tool: {t.get('tool_name')} | Args: {t.get('tool_args')} | Result: {t.get('tool_result_summary')}"
        for i, t in enumerate(traces)
    ])
    
    prompt = f"""
You are an Autoresearch Process Critic. Your job is to strictly evaluate an AI agent's execution process, completely ignoring the final stock price outcome.

Review the following execution trace:
{trace_summary}

Score the process from 0 to 100 based on:
1. Logic & Sourcing: Are decisions logically sound and backed by the tool data?
2. Tool Usage Accuracy: Did the agent use the right tools with valid arguments? Were there any hallucinations or syntax errors?

Respond in JSON format:
{{
    "process_score": <float 0-100>,
    "logic_score": <float 0-100>,
    "tool_score": <float 0-100>,
    "feedback": "<detailed critique>"
}}
"""
    
    try:
        caller = PrismAgentCaller(agent_id="autoresearch")
        response = await caller.query(prompt)
        
        # Parse JSON from response
        start_idx = response.find("{")
        end_idx = response.rfind("}") + 1
        if start_idx != -1 and end_idx != 0:
            result = json.loads(response[start_idx:end_idx])
            
            # Ensure float types
            result["process_score"] = float(result.get("process_score", 0.0))
            result["logic_score"] = float(result.get("logic_score", 0.0))
            result["tool_score"] = float(result.get("tool_score", 0.0))
            
            return result
        else:
            return {"process_score": 50.0, "logic_score": 50.0, "tool_score": 50.0, "feedback": f"Failed to parse JSON: {response}"}
    except Exception as e:
        logger.error("Critic evaluation failed: %s", e)
        return {"process_score": 0.0, "logic_score": 0.0, "tool_score": 0.0, "feedback": f"Error: {str(e)}"}
