import logging
import json
import asyncio
from collections import defaultdict
from app.tools.registry import registry, current_agent_name
from app.tools.executor import run_tool_agent, AgentYielded
from app.services.prism_agent_caller import llm, Priority
from app.services.prism_agent_caller import call_prism_agent

logger = logging.getLogger(__name__)

SUBAGENT_SYSTEM_PROMPT = """You are an expert Research Subagent. 
Your goal is to gather specific information requested by the primary Analyst Agent.
You have access to web search, url scraping, and specialized models (Hermes).
CRITICAL: To prevent context overflow when analyzing huge datasets or scraped pages, you MUST prioritize using the `grep_search_text` and `paginated_read` tools rather than returning or reading entire raw documents.
1. Break down the task.
2. Use tools to gather data (use grep/pagination for large texts).
3. Formulate a concise, highly factual summary.
Output your final answer as JSON in the following format:
{
  "status": "success|failed",
  "summary": "Your detailed findings here. Include numbers, facts, and sources.",
  "confidence": 0-100
}
"""

YIELD_SUMMARY_PROMPT = """You were a research subagent that ran out of execution steps before finishing.
Below is your conversation so far. Summarize ALL information you have gathered into a final JSON response.
Even partial data is valuable — report what you found.

Output your answer as JSON:
{
  "status": "partial",
  "summary": "Everything you discovered so far, with numbers and sources.",
  "confidence": 0-100,
  "note": "What was left unfinished"
}
"""

