"""
Quant Equation Tools — Agent-callable tools for the Equation Library.

Registers tools that debate/tournament agents can call to:
  - search_equations: Find existing equations by keyword
  - save_equation: Store a new equation in the library
  - run_equation: Execute an equation against live price data
  - run_backtest: Backtest an equation over historical data
"""

import json
import logging
from pydantic import BaseModel, Field
from app.tools.registry import registry

logger = logging.getLogger(__name__)


# ── Input Models ────────────────────────────────────────────────────

class SearchEquationsInput(BaseModel):
    query: str = Field(
        default="",
        description="Search keyword to find equations by name or description. Empty returns all.",
    )
    top_k: int = Field(
        default=10,
        description="Maximum number of equations to return.",
    )


class SaveEquationInput(BaseModel):
    name: str = Field(
        description="Unique name for the equation (e.g., 'zscore_mean_reversion', 'rsi_macd_crossover'). Use snake_case.",
    )
    code: str = Field(
        description=(
            "Python code for the equation. Must use 'df' (price DataFrame with columns: "
            "open, high, low, close, volume, rsi_14, macd, macd_signal, macd_hist, atr_14, z_score) "
            "and 'params' (dict of parameters). Must assign the output to 'result'. "
            "Example: 'result = {\"signal\": \"BUY\" if df[\"z_score\"].iloc[-1] < -2.0 else \"HOLD\", "
            "\"z_score\": float(df[\"z_score\"].iloc[-1])}'"
        ),
    )
    description: str = Field(
        description="Human-readable description of what this equation does and when to use it.",
    )
    parameters: str = Field(
        default="{}",
        description="JSON string of default parameters (e.g., '{\"entry_z\": -2.0, \"exit_z\": 0.0}').",
    )


class RunEquationInput(BaseModel):
    ticker: str = Field(
        description="The stock ticker symbol to run the equation against (e.g., AAPL).",
    )
    equation_name: str = Field(
        default="",
        description="Name of a saved equation from the library. If empty, 'code' must be provided.",
    )
    code: str = Field(
        default="",
        description="Raw Python code to execute (if not using a named equation). Must assign to 'result'.",
    )
    parameters: str = Field(
        default="{}",
        description="JSON string of parameters to pass to the equation.",
    )


class RunBacktestInput(BaseModel):
    ticker: str = Field(
        description="The stock ticker symbol to backtest against.",
    )
    equation_name: str = Field(
        description="Name of the equation from the library to backtest.",
    )
    parameters: str = Field(
        default="{}",
        description="JSON string of parameters for the backtest.",
    )


# ── Tool Registration ──────────────────────────────────────────────

@registry.register(
    name="search_equations",
    description=(
        "Search the shared Quant Equation Library for existing mathematical strategies. "
        "Returns equations with their win rates, Sharpe ratios, and code. "
        "Use this before writing a new equation to check if one already exists."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keyword (e.g., 'zscore', 'momentum', 'rsi'). Empty returns top equations by win rate.",
            },
            "top_k": {
                "type": "integer",
                "description": "Max results to return (default: 10).",
            },
        },
        "required": [],
    },
    tier=0,
    source="equation_library",
    input_model=SearchEquationsInput,
)
async def search_equations_tool(query: str = "", top_k: int = 10) -> str:
    from app.cognition.debate.equation_library import search_equations

    results = search_equations(query, top_k)
    if not results:
        return "No equations found in the library. You can create one with save_equation."

    lines = [f"Found {len(results)} equations:\n"]
    for eq in results:
        lines.append(
            f"  📐 {eq['name']}: {eq['description'][:100]}\n"
            f"     Win Rate: {eq['win_rate_pct']}% | Sharpe: {eq['sharpe_ratio']:.2f} | "
            f"Uses: {eq['usage_count']} | Author: {eq['author_agent']}\n"
            f"     Code: {eq['code'][:200]}...\n"
        )
    return "\n".join(lines)


@registry.register(
    name="save_equation",
    description=(
        "Save a new mathematical equation to the shared Quant Equation Library. "
        "The equation must be Python code that receives 'df' (price DataFrame) and 'params' (dict), "
        "and assigns its output to 'result'. Future agents can reuse this equation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique snake_case name for the equation.",
            },
            "code": {
                "type": "string",
                "description": "Python code. Must assign output to 'result'. Has access to df, params, np, pd.",
            },
            "description": {
                "type": "string",
                "description": "What this equation does and when to use it.",
            },
            "parameters": {
                "type": "string",
                "description": "JSON string of default parameters.",
            },
        },
        "required": ["name", "code", "description"],
    },
    tier=0,
    source="equation_library",
    input_model=SaveEquationInput,
)
async def save_equation_tool(
    name: str,
    code: str,
    description: str,
    parameters: str = "{}",
    **kwargs,
) -> str:
    from app.cognition.debate.equation_library import save_equation

    try:
        params = json.loads(parameters)
    except (json.JSONDecodeError, TypeError):
        params = {}

    agent_name = kwargs.get("_agent_name", "unknown")
    ticker = kwargs.get("_ticker", "")

    result = save_equation(
        name=name,
        code=code,
        description=description,
        parameters=params,
        author_agent=agent_name,
        ticker_origin=ticker,
    )

    if "error" in result:
        return f"Failed to save equation: {result['error']}"

    return f"Equation '{name}' saved successfully to the shared library. ID: {result['id']}"


@registry.register(
    name="run_equation",
    description=(
        "Execute a mathematical equation against live price/technical data for a ticker. "
        "You can run a named equation from the library OR provide raw Python code. "
        "The code receives 'df' (DataFrame with OHLCV + technicals) and 'params' (dict). "
        "Must assign output to 'result'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker to run against (e.g., AAPL).",
            },
            "equation_name": {
                "type": "string",
                "description": "Name of a saved equation. If empty, 'code' must be provided.",
            },
            "code": {
                "type": "string",
                "description": "Raw Python code if not using a named equation.",
            },
            "parameters": {
                "type": "string",
                "description": "JSON string of parameters.",
            },
        },
        "required": ["ticker"],
    },
    tier=0,
    source="equation_library",
    input_model=RunEquationInput,
)
async def run_equation_tool(
    ticker: str,
    equation_name: str = "",
    code: str = "",
    parameters: str = "{}",
    **kwargs,
) -> str:
    from app.cognition.debate.equation_library import execute_equation, get_equation_by_name

    try:
        params = json.loads(parameters)
    except (json.JSONDecodeError, TypeError):
        params = {}

    # If using a named equation, fetch its code
    if equation_name:
        eq = get_equation_by_name(equation_name)
        if not eq:
            return f"Equation '{equation_name}' not found in library. Use search_equations to find available ones."
        code = eq["code"]
        # Merge default params with provided params
        eq_params = eq.get("parameters", {})
        if isinstance(eq_params, str):
            try:
                eq_params = json.loads(eq_params)
            except (json.JSONDecodeError, TypeError):
                eq_params = {}
        merged_params = {**eq_params, **params}
        params = merged_params

    if not code:
        return "Either equation_name or code must be provided."

    result = execute_equation(code, ticker, params)

    if "error" in result:
        return f"Equation execution failed: {result['error']}"

    output = json.dumps(result.get("result", {}), indent=2, default=str)
    response = f"Equation executed successfully for {ticker}:\n{output}"
    if result.get("stdout"):
        response += f"\n\nStdout:\n{result['stdout']}"
    return response


@registry.register(
    name="run_backtest",
    description=(
        "Backtest a named equation from the library over historical price data. "
        "Returns performance metrics: PnL, win rate, max drawdown, Sharpe ratio, and trade list."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker to backtest against.",
            },
            "equation_name": {
                "type": "string",
                "description": "Name of the equation from the library to backtest.",
            },
            "parameters": {
                "type": "string",
                "description": "JSON string of parameters for the backtest.",
            },
        },
        "required": ["ticker", "equation_name"],
    },
    tier=0,
    source="equation_library",
    input_model=RunBacktestInput,
)
async def run_backtest_tool(
    ticker: str,
    equation_name: str,
    parameters: str = "{}",
    **kwargs,
) -> str:
    from app.cognition.debate.backtest_runner import run_backtest_for_equation

    try:
        params = json.loads(parameters)
    except (json.JSONDecodeError, TypeError):
        params = {}

    result = run_backtest_for_equation(equation_name, ticker, params)

    if "error" in result:
        return f"Backtest failed: {result['error']}"

    # Format summary
    lines = [f"Backtest Results for '{equation_name}' on {ticker}:"]
    lines.append(f"  Total Trades: {result.get('total_trades', 0)}")
    lines.append(f"  Win Rate: {result.get('win_rate_pct', 0)}%")
    lines.append(f"  Avg Return: {result.get('average_return_pct', 0):.3f}%")
    lines.append(f"  Cumulative PnL: {result.get('cumulative_return_pct', 0):.2f}%")
    lines.append(f"  Max Drawdown: {result.get('max_drawdown_pct', 0):.2f}%")

    # Show recent trades
    trades = result.get("trades", [])
    if trades:
        lines.append(f"\n  Recent Trades (last 5):")
        for t in trades[-5:]:
            lines.append(
                f"    {t.get('entry_date', '?')} → {t.get('exit_date', '?')}: "
                f"${t.get('entry_price', 0):.2f} → ${t.get('exit_price', 0):.2f} "
                f"({t.get('return_pct', 0):+.2f}%)"
            )

    return "\n".join(lines)
