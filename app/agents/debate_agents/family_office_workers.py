"""
Family Office V3 — Worker Analysts.

Specialized worker agents that the Worker Orchestrator dispatches
to fetch specific data for PMs. Each worker has dynamic open access
to its role-specific tool subset and autonomously decides which
tools to call.

Workers are lightweight — they fetch data and return it, they don't
analyze or reason. Analysis is the PM's job.

All LLM calls go through app.services.prism_agent_caller (Rule 2).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.cognition.contracts.family_office import (
    DataRequest,
    WorkerResult,
    WorkerType,
)
from app.services.prism_agent_caller import llm, Priority

logger = logging.getLogger(__name__)

import time

class SimpleTTLCache:
    def __init__(self, maxsize=1000, ttl=3600):
        self.maxsize = maxsize
        self.ttl = ttl
        self.cache = {}
        
    def __getitem__(self, key):
        if key in self.cache:
            val, expiry = self.cache[key]
            if expiry > time.time():
                return val
            else:
                del self.cache[key]
        raise KeyError(key)
        
    def __setitem__(self, key, value):
        now = time.time()
        expired = [k for k, (v, exp) in self.cache.items() if exp <= now]
        for k in expired:
            del self.cache[k]
        if len(self.cache) >= self.maxsize:
            oldest = next(iter(self.cache))
            del self.cache[oldest]
        self.cache[key] = (value, now + self.ttl)
        
    def __contains__(self, key):
        try:
            self[key]
            return True
        except KeyError:
            return False
            
    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default
            
    def pop(self, key, default=None):
        try:
            val, _ = self.cache.pop(key)
            return val
        except KeyError:
            return default

# State to track rate limits per cycle
_search_web_calls = SimpleTTLCache(maxsize=1000, ttl=3600)


# ── Worker System Prompts ───────────────────────────────────────────────

WORKER_PROMPTS: dict[WorkerType, str] = {
    WorkerType.QUANT: """You are a Quantitative Data Analyst worker. Your ONLY job is to fetch numerical market data.

INSTRUCTIONS:
1. Read the data request below.
2. Use your tools to fetch the EXACT data requested.
3. Return the raw data in a structured format.
4. Do NOT analyze or interpret the data — just fetch it.
5. If a tool fails or returns no data, report that clearly.

TOOLS AVAILABLE: get_market_data, get_technical_indicators, get_polygon_price_history, get_options_flow, query_technical_indicator

Output the data you fetched. Be precise with numbers. Include the source tool name.""",

    WorkerType.FUNDAMENTAL: """You are a Fundamental Data Analyst worker. Your ONLY job is to fetch company financial data.

INSTRUCTIONS:
1. Read the data request below.
2. Use your tools to fetch the EXACT financial data requested.
3. Return the raw data in a structured format.
4. Do NOT analyze or interpret the data — just fetch it.
5. If a tool fails or returns no data, report that clearly.

TOOLS AVAILABLE: get_market_data, get_finviz_fundamentals, get_sec_filings, get_earnings_data, query_financial_metrics, search_database_facts

Output the data you fetched. Be precise with numbers. Include the source tool name.""",

    WorkerType.NEWS: """You are a News & Sentiment Data Analyst worker. Your ONLY job is to fetch news and sentiment data.

INSTRUCTIONS:
1. Read the data request below.
2. Use your tools to fetch the EXACT news/sentiment data requested.
3. Return the raw data in a structured format.
4. Do NOT analyze or interpret the data — just fetch it.
5. If a tool fails or returns no data, report that clearly.

TOOLS AVAILABLE: get_finnhub_news, search_web, search_database_facts, search_internal_database

Output the data you fetched. Include headlines, dates, and sentiment scores where available.""",

    WorkerType.INSIDER: """You are an Insider Activity Data Analyst worker. Your ONLY job is to fetch insider/institutional trading data.

INSTRUCTIONS:
1. Read the data request below.
2. Use your tools to fetch the EXACT insider/institutional data requested.
3. Return the raw data in a structured format.
4. Do NOT analyze or interpret the data — just fetch it.
5. If a tool fails or returns no data, report that clearly.

TOOLS AVAILABLE: get_insider_trades, get_congress_trades, get_sec_filings, search_database_facts

Output the data you fetched. Include dates, transaction types, and amounts.""",
}


# ── Worker Tool Whitelists ──────────────────────────────────────────────
# These are the tool names each worker type is allowed to use.
# The actual schemas are resolved at runtime from the registry.

WORKER_TOOL_NAMES: dict[WorkerType, list[str]] = {
    WorkerType.QUANT: [
        "get_market_data",
        "get_technical_indicators",
        "get_polygon_price_history",
        "get_options_flow",
        "query_technical_indicator",
    ],
    WorkerType.FUNDAMENTAL: [
        "get_market_data",
        "get_finviz_fundamentals",
        "get_sec_filings",
        "get_earnings_data",
        "query_financial_metrics",
        "search_database_facts",
    ],
    WorkerType.NEWS: [
        "get_finnhub_news",
        "search_web",
        "search_database_facts",
        "search_internal_database",

    ],
    WorkerType.INSIDER: [
        "get_insider_trades",
        "get_congress_trades",
        "get_sec_filings",
        "search_database_facts",
    ],
}


def _get_worker_tool_schemas(worker_type: WorkerType, cycle_id: str) -> list[dict]:
    """Resolve tool schemas for a worker type from the registry."""
    from app.tools.registry import registry

    tool_names = WORKER_TOOL_NAMES.get(worker_type, []).copy()
    
    # Rate limit search_web (max 3 per cycle)
    if "search_web" in tool_names:
        calls = _search_web_calls.get(cycle_id, 0)
        if calls >= 3:
            logger.warning(f"[V3] Rate limiting search_web for cycle {cycle_id} (calls: {calls})")
            tool_names.remove("search_web")

    schemas = registry.get_schemas_by_names(tool_names)
    return schemas if schemas else []


async def dispatch_worker(
    request: DataRequest,
    cycle_id: str,
    bot_id: str,
) -> WorkerResult:
    """Dispatch a single worker to fetch data for a DataRequest.

    The worker autonomously decides which of its whitelisted tools
    to call based on the natural language description in the request.
    """
    from app.config.config_cognition import cognition_settings

    worker_type = request.worker_type
    system_prompt = WORKER_PROMPTS.get(worker_type, "")
    if not system_prompt:
        return WorkerResult(
            worker_type=worker_type,
            request_description=request.description,
            data="",
            success=False,
            error=f"No prompt defined for worker type: {worker_type.value}",
        )

    user_prompt = f"""## DATA REQUEST
Ticker: {request.ticker}
Priority: {request.priority}
Description: {request.description}
Specific metrics needed: {', '.join(request.specific_metrics) if request.specific_metrics else 'Not specified'}

Fetch this data using your tools and return the results."""

    tool_schemas = _get_worker_tool_schemas(worker_type, cycle_id)
    if not tool_schemas:
        logger.warning("[V3] Worker %s has no tool schemas — cannot fetch data", worker_type.value)
        return WorkerResult(
            worker_type=worker_type,
            request_description=request.description,
            data="",
            success=False,
            error="No tools available for this worker type",
        )

    agent_name = f"v3_{worker_type.value}"
    tool_calls_made = []

    try:
        # Use the local agent loop with tools for workers
        # Workers get a tight budget — they just fetch data
        from lazycat.agent import BaseAgent, AgentHarness
        from lazycat.session import ConversationSession
        import time

        timeout_s = float(getattr(cognition_settings, "V3_WORKER_TIMEOUT_SECONDS", 60))
        
        from app.services.prism_agent_caller import llm
        agent = BaseAgent(
            name=agent_name, 
            system_prompt=system_prompt,
            llm_client=llm.prism_client,
            project="vllm-trading-bot"
        )
        for t in tool_schemas:
            agent.add_tool(t)
            
        session = ConversationSession(session_id=f"worker_{int(time.time())}")
        harness = AgentHarness(agent=agent, session=session)
        harness.max_iterations = 3

        async def run_harness():
            return await harness.run(user_prompt)

        final_text = await asyncio.wait_for(
            run_harness(),
            timeout=timeout_s,
        )
        result = {"final_text": final_text}

        final_text = result.get("final_text", "")
        tokens_used = result.get("token_usage", 0)

        # Track which tools were called
        for step in result.get("steps", []):
            if isinstance(step, dict) and step.get("tool_name"):
                tool_name = step["tool_name"]
                tool_calls_made.append(tool_name)
                if tool_name == "search_web":
                    _search_web_calls[cycle_id] = _search_web_calls.get(cycle_id, 0) + 1

        logger.info(
            "[V3] Worker %s fetched data for %s: %d tokens, %d tools called",
            worker_type.value, request.ticker, tokens_used, len(tool_calls_made),
        )

        return WorkerResult(
            worker_type=worker_type,
            request_description=request.description,
            data=final_text[:5000],  # Cap data to prevent context bloat
            source=agent_name,
            success=bool(final_text.strip()),
            tool_calls_made=tool_calls_made,
        )

    except asyncio.TimeoutError:
        logger.warning(
            "[V3] Worker %s timed out for %s", worker_type.value, request.ticker,
        )
        return WorkerResult(
            worker_type=worker_type,
            request_description=request.description,
            data="",
            success=False,
            error=f"Worker timed out after {timeout_s}s",
        )
    except Exception as e:
        logger.error(
            "[V3] Worker %s failed for %s: %s", worker_type.value, request.ticker, e,
        )
        return WorkerResult(
            worker_type=worker_type,
            request_description=request.description,
            data="",
            success=False,
            error=str(e),
        )


async def dispatch_workers_parallel(
    requests: list[DataRequest],
    cycle_id: str,
    bot_id: str,
) -> list[WorkerResult]:
    """Dispatch multiple workers in parallel and collect results.

    Deduplicates requests by (worker_type, description) to avoid
    fetching the same data twice.
    """
    from app.services.adaptive_concurrency import concurrency_controller

    if not requests:
        return []

    # Deduplicate by (worker_type, description)
    seen: set[tuple[str, str]] = set()
    deduped: list[DataRequest] = []
    for req in requests:
        key = (req.worker_type.value, req.description[:100])
        if key not in seen:
            seen.add(key)
            deduped.append(req)

    if len(deduped) < len(requests):
        logger.info(
            "[V3] Deduped %d→%d worker requests", len(requests), len(deduped),
        )

    tasks = [dispatch_worker(req, cycle_id, bot_id) for req in deduped]
    results = await concurrency_controller.gather(
        tasks, label="v3_workers", return_exceptions=True,
    )

    worker_results = []
    for r in results:
        if isinstance(r, BaseException):
            logger.error("[V3] Worker dispatch failed: %s", r)
            worker_results.append(WorkerResult(
                worker_type=WorkerType.FUNDAMENTAL,
                request_description="failed",
                data="",
                success=False,
                error=str(r),
            ))
        else:
            worker_results.append(r)

    return worker_results
