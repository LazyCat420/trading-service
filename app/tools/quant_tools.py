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
            "open, high, low, close, volume, rsi_14, macd, macd_signal, macd_hist, atr_14, z_score, "
            "gk_vol, mom_21d, mom_63d, mom_126d, mom_252d) "
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


# ── Volatility forecasting ─────────────────────────────────────────
# GARCH lives here as a TOOL, not a library equation: the equation sandbox
# only allows numpy/pandas imports (no arch, no scipy), so a saved
# "garch_vol_forecast" equation would die on its import line every run.

@registry.register(
    name="forecast_volatility_garch",
    description=(
        "Fit GARCH(1,1) on the ticker's daily returns and forecast NEXT-DAY "
        "volatility — a forward-looking volatility estimate, unlike ATR which "
        "only describes the past. Returns predicted vs realized (20d) "
        "annualized vol, the prediction premium, and vol_signal: EXPANSION "
        "(model expects vol to rise — widen stops, shrink size), CONTRACTION, "
        "or NEUTRAL. Use this to ground volatility_regime in a forecast "
        "rather than trailing ATR alone."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Stock ticker (e.g. AAPL)."},
            "lookback_days": {
                "type": "integer",
                "description": "Trading days of history to fit on (default 500).",
            },
        },
        "required": ["ticker"],
    },
    tier=0,
    source="portfolio_math",
)
async def forecast_volatility_garch_tool(
    ticker: str, lookback_days: int = 500, **_extra
) -> str:
    from app.quant.garch import garch_forecast
    from app.quant.returns import load_close_returns

    returns = load_close_returns(ticker, int(lookback_days))
    if returns.size == 0:
        return json.dumps({"error": f"No price history for {ticker}."})
    result = garch_forecast(returns)
    result["ticker"] = ticker.upper()
    return json.dumps(result)


# ── Risk calculators ───────────────────────────────────────────────
# Whitelisted for the quant analyst, tournament pitches, and user chat since
# the whitelists were written, but never implemented — every call errored.

@registry.register(
    name="calculate_stop_loss",
    description=(
        "Calculate a stop-loss price from ATR (Average True Range) volatility: "
        "stop_loss = entry_price - (ATR * multiplier). Get the ATR from "
        "get_technical_indicators first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "entry_price": {"type": "number", "description": "Planned entry price per share"},
            "atr": {"type": "number", "description": "Current ATR (Average True Range) value"},
            "multiplier": {"type": "number", "description": "ATR multiplier (default 2.0, higher = wider stop)"},
        },
        "required": ["entry_price", "atr"],
    },
    tier=0,
    source="risk_calculator",
)
async def calculate_stop_loss_tool(entry_price: float, atr: float, multiplier: float = 2.0, **_extra) -> str:
    entry_price, atr, multiplier = float(entry_price), float(atr), float(multiplier or 2.0)
    stop = entry_price - (atr * multiplier)
    if stop <= 0:
        return json.dumps({
            "error": f"ATR {atr} x multiplier {multiplier} exceeds the entry price {entry_price}; "
                     "use a smaller multiplier.", "is_error": True})
    return json.dumps({
        "stop_loss_price": round(stop, 2),
        "stop_distance": round(entry_price - stop, 2),
        "stop_distance_pct": round(((entry_price - stop) / entry_price) * 100, 2),
        "entry_price": entry_price, "atr": atr, "multiplier": multiplier,
    })


@registry.register(
    name="calculate_risk_reward",
    description=(
        "Calculate the risk-to-reward ratio for a trade setup: "
        "(target - entry) / (entry - stop). A ratio >= 2.0 is generally favorable."
    ),
    parameters={
        "type": "object",
        "properties": {
            "entry_price": {"type": "number", "description": "Planned entry price per share"},
            "target_price": {"type": "number", "description": "Price target for profit taking"},
            "stop_loss_price": {"type": "number", "description": "Stop-loss price for the trade (alias: stop_loss)"},
            "stop_loss": {"type": "number", "description": "Alternative key for stop-loss price"},
        },
        "required": ["entry_price", "target_price"],
    },
    tier=0,
    source="risk_calculator",
)
async def calculate_risk_reward_tool(
    entry_price: float, target_price: float,
    stop_loss_price: float | None = None, stop_loss: float | None = None, **_extra,
) -> str:
    stop = stop_loss_price if stop_loss_price is not None else stop_loss
    if stop is None:
        return json.dumps({"error": "Provide stop_loss_price (or stop_loss).", "is_error": True})
    entry_price, target_price, stop = float(entry_price), float(target_price), float(stop)
    risk = entry_price - stop
    reward = target_price - entry_price
    if risk <= 0:
        return json.dumps({"error": "Stop-loss must be below the entry price for a long setup.", "is_error": True})
    ratio = reward / risk
    return json.dumps({
        "risk_reward_ratio": round(ratio, 2),
        "risk_per_share": round(risk, 2),
        "reward_per_share": round(reward, 2),
        "favorable": ratio >= 2.0,
        "entry_price": entry_price, "target_price": target_price, "stop_loss_price": stop,
    })


@registry.register(
    name="calculate_position_size",
    description=(
        "Calculate the number of shares to buy with the fixed-risk method: "
        "risk_amount = cash * risk_percent, shares = risk_amount / (entry - stop). "
        "Caps the resulting notional at available cash."
    ),
    parameters={
        "type": "object",
        "properties": {
            "cash_available": {"type": "number", "description": "Total cash available for trading (e.g., 100000)"},
            "risk_percent": {"type": "number", "description": "Max percentage of cash to risk on this trade (e.g., 0.02 for 2%)"},
            "entry_price": {"type": "number", "description": "Planned entry price per share (e.g., 150.50)"},
            "stop_loss_price": {"type": "number", "description": "Planned stop-loss price per share (e.g., 142.00)"},
        },
        "required": ["cash_available", "risk_percent", "entry_price", "stop_loss_price"],
    },
    tier=0,
    source="risk_calculator",
)
async def calculate_position_size_tool(
    cash_available: float, risk_percent: float, entry_price: float, stop_loss_price: float, **_extra,
) -> str:
    cash, risk_pct = float(cash_available), float(risk_percent)
    entry, stop = float(entry_price), float(stop_loss_price)
    if risk_pct > 1.0:  # tolerate "2" meaning 2%
        risk_pct = risk_pct / 100.0
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return json.dumps({"error": "Stop-loss must be below the entry price for a long setup.", "is_error": True})
    risk_amount = cash * risk_pct
    shares = int(risk_amount / risk_per_share)
    max_affordable = int(cash / entry) if entry > 0 else 0
    shares = min(shares, max_affordable)
    return json.dumps({
        "shares": shares,
        "notional_value": round(shares * entry, 2),
        "risk_amount": round(risk_amount, 2),
        "risk_per_share": round(risk_per_share, 2),
        "capped_by_cash": shares == max_affordable and max_affordable > 0,
    })
