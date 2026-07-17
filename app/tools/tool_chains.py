"""tool_chains.py — named tool chains: bundle N tool calls into one agent call.

Motivation: agents were firing the same handful of tools one-by-one every run
(e.g. market_data -> indicators -> news -> fundamentals for a single ticker).
Each round-trip costs a full LLM turn (tokens + latency). A *tool chain* is a
named, config-defined sequence the agent invokes with ONE call to
``run_tool_chain``; the steps run in-process here (no per-step LLM turn) and the
consolidated result comes back in a single tool response.

Design:
  * ``TOOL_CHAINS`` is plain config (below) — add/modify chains without touching
    execution logic. Each chain is an ordered list of steps.
  * Every tool in a chain is ALSO callable standalone — chains are a shortcut,
    never a replacement.
  * Steps execute through ``registry.execute_tool_call(..., force_local=True)``
    so name-normalization, permission checks, input validation, and telemetry
    all still apply per step. Running force_local keeps every step in this
    process instead of bouncing back out to the lazy-tool gateway.
  * Arg templating: a step arg whose value is a string starting with ``$`` is
    resolved against a context of {chain params + prior step outputs}. E.g.
    ``"$ticker"`` pulls the chain param ``ticker``; ``"$step_0.price"`` or
    ``"$get_market_data.price"`` pulls a field from an earlier step's parsed
    JSON output. This is how "run tool 1 -> feed tool 2" works.
"""

import json
import logging

from app.tools.registry import registry, PermissionLevel

logger = logging.getLogger(__name__)


# ── Chain catalog ───────────────────────────────────────────────────────────
# Each chain: {description, params:[names the agent should supply], steps:[...]}
# Each step: {tool: <registered tool name>, args: {arg: value|"$ref"}}
TOOL_CHAINS: dict[str, dict] = {
    "ticker_deep_dive": {
        "description": (
            "Full single-ticker data pull in one shot: market data, technical "
            "indicators, latest news, and fundamentals. Feed it a ticker."
        ),
        "params": ["ticker"],
        "steps": [
            {"tool": "get_market_data", "args": {"ticker": "$ticker"}},
            {"tool": "get_technical_indicators", "args": {"ticker": "$ticker"}},
            {"tool": "get_finnhub_news", "args": {"ticker": "$ticker"}},
            {"tool": "get_finviz_fundamentals", "args": {"ticker": "$ticker"}},
        ],
    },
    "chart_with_context": {
        "description": (
            "Render a trading chart for a ticker and pull the market data + news "
            "that explain it. Params: ticker, overlays (array for save_trading_chart)."
        ),
        "params": ["ticker", "overlays"],
        "steps": [
            {"tool": "save_trading_chart", "args": {"ticker": "$ticker", "overlays": "$overlays"}},
            {"tool": "get_market_data", "args": {"ticker": "$ticker"}},
            {"tool": "get_finnhub_news", "args": {"ticker": "$ticker"}},
        ],
    },
    "news_and_fundamentals": {
        "description": (
            "Lighter single-ticker read: latest news + fundamentals only. Feed it "
            "a ticker. Use when you don't need price/indicator detail."
        ),
        "params": ["ticker"],
        "steps": [
            {"tool": "get_finnhub_news", "args": {"ticker": "$ticker"}},
            {"tool": "get_finviz_fundamentals", "args": {"ticker": "$ticker"}},
        ],
    },
    "morning_market_pulse": {
        "description": (
            "Market-wide open snapshot: Reddit-trending tickers with sentiment + "
            "the market map. No params."
        ),
        "params": [],
        "steps": [
            {"tool": "get_reddit_trending_stocks", "args": {"limit": 25}},
            {"tool": "get_market_map_data", "args": {}},
        ],
    },
}


def _resolve(value, context: dict):
    """Resolve ``$dotted.path`` references against context; recurse dict/list."""
    if isinstance(value, str) and value.startswith("$"):
        cur = context
        for part in value[1:].split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
        return cur
    if isinstance(value, dict):
        return {k: _resolve(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, context) for v in value]
    return value


def _catalog() -> dict:
    """Agent-facing summary of available chains."""
    return {
        name: {"description": c["description"], "params": c.get("params", [])}
        for name, c in TOOL_CHAINS.items()
    }


@registry.register(
    name="run_tool_chain",
    description=(
        "Run a named, pre-defined sequence of tools in ONE call instead of "
        "calling each tool separately (saves turns/tokens). Pass 'chain' (the "
        "chain name) and 'params' (its inputs, e.g. {\"ticker\": \"NVDA\"}). "
        "Omit 'chain' or pass an unknown name to get the catalog of available "
        "chains and their params. Available: ticker_deep_dive, chart_with_context, "
        "news_and_fundamentals, morning_market_pulse. Each underlying tool is also "
        "callable on its own."
    ),
    parameters={
        "type": "object",
        "properties": {
            "chain": {
                "type": "string",
                "enum": list(TOOL_CHAINS.keys()),
                "description": "Name of the chain to run. Omit to list available chains.",
            },
            "params": {
                "type": "object",
                "description": "Inputs for the chain (e.g. {\"ticker\": \"AAPL\"}). See each chain's params.",
            },
        },
        "required": [],
    },
    permission=PermissionLevel.READ_ONLY,
    tags=["chain", "workflow", "bundle", "orchestration"],
    domain="Research & Intelligence",
    labels=["chain", "orchestration"],
    concurrency_safe=True,
)
async def run_tool_chain(chain: str | None = None, params: dict | None = None, **_extra) -> str:
    """Execute a named tool chain and return the consolidated results.

    Returns JSON: ``{status, chain, executed:[{step, tool, args, ok, result}],
    errors:[...]}``. On unknown/missing chain, returns the catalog.
    """
    params = params or {}
    # Tolerate a stray JSON string for params.
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}

    if not chain or chain not in TOOL_CHAINS:
        return json.dumps({
            "status": "catalog" if not chain else "error",
            "message": None if not chain else f"Unknown chain '{chain}'.",
            "available_chains": _catalog(),
        })

    spec = TOOL_CHAINS[chain]
    context: dict = dict(params)
    executed: list[dict] = []
    errors: list[str] = []

    logger.info("[ToolChains] Running chain '%s' with params=%s", chain, list(params))

    for i, step in enumerate(spec["steps"]):
        tool_name = step["tool"]
        args = _resolve(step.get("args", {}), context)
        tool_call = {
            "id": f"chain-{chain}-{i}",
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(args)},
        }
        try:
            result = await registry.execute_tool_call(
                tool_call,
                force_local=True,
                skip_permission_check=True,
                agent_name=f"tool_chain:{chain}",
            )
            content = result.get("content", "")
            # Parse for both piping and a clean nested result; keep raw on failure.
            try:
                parsed = json.loads(content) if isinstance(content, str) else content
            except (json.JSONDecodeError, TypeError):
                parsed = content
            ok = not (isinstance(parsed, dict) and ("error" in parsed or parsed.get("status") == "error"))
            if not ok:
                errors.append(f"{tool_name}: {parsed.get('error') or parsed.get('message') if isinstance(parsed, dict) else parsed}")
            # Expose this step's output to later steps.
            context[f"step_{i}"] = parsed
            context[tool_name] = parsed
            executed.append({"step": i, "tool": tool_name, "args": args, "ok": ok, "result": parsed})
        except Exception as e:
            logger.error("[ToolChains] Step %d (%s) crashed: %s", i, tool_name, e, exc_info=True)
            errors.append(f"{tool_name}: {e}")
            executed.append({"step": i, "tool": tool_name, "args": args, "ok": False, "result": {"error": str(e)}})

    return json.dumps({
        "status": "success" if not errors else "partial",
        "chain": chain,
        "executed": executed,
        "errors": errors,
    })
