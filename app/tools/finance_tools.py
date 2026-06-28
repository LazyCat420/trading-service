from lazycat.tools import tool_executor

async def get_market_data(ticker: str) -> str:
    res = await tool_executor.execute_tool("get_market_data", {"ticker": ticker})
    return str(res.get("result", res) if isinstance(res, dict) else res)

async def get_finnhub_news(ticker: str) -> str:
    res = await tool_executor.execute_tool("get_finnhub_news", {"ticker": ticker})
    return str(res.get("result", res) if isinstance(res, dict) else res)

async def get_technical_indicators(ticker: str) -> str:
    res = await tool_executor.execute_tool("get_technical_indicators", {"ticker": ticker})
    return str(res.get("result", res) if isinstance(res, dict) else res)
