from lazycat.tools import tool_executor

async def hermes_web_research(query: str) -> str:
    res = await tool_executor.execute_tool("search_web", {"query": query})
    return str(res.get("result", res) if isinstance(res, dict) else res)
