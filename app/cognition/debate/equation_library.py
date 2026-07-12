"""
Equation Library — Shared Quant Equation Storage & Sandboxed Executor.

Allows debate agents to:
  1. Search for existing equations by keyword
  2. Save new profitable equations they discover
  3. Execute equations in a sandboxed Python environment against price data

All equations are stored in the `quant_equation_library` PostgreSQL table.
"""

import logging
import json
import uuid
import io
import contextlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# ── Sandbox Safety ───────────────────────────────────────────────────
# Only these modules are available inside sandboxed equation execution.
SAFE_GLOBALS = {
    "__builtins__": {
        # Math/logic
        "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
        "len": len, "range": range, "enumerate": enumerate, "zip": zip,
        "sorted": sorted, "reversed": reversed, "filter": filter, "map": map,
        "isinstance": isinstance, "float": float, "int": int, "str": str,
        "bool": bool, "list": list, "dict": dict, "tuple": tuple, "set": set,
        "True": True, "False": False, "None": None,
        "print": lambda *a, **kw: None,  # Silently swallow prints
        "ValueError": ValueError, "TypeError": TypeError,
        "KeyError": KeyError, "IndexError": IndexError,
        "Exception": Exception, "ZeroDivisionError": ZeroDivisionError,
    },
    "np": np,
    "pd": pd,
}

# Maximum execution time for sandboxed code (seconds)
SANDBOX_TIMEOUT_SEC = 10
# Maximum code length
MAX_CODE_LENGTH = 5000


# ── Database Operations ─────────────────────────────────────────────

def save_equation(
    name: str,
    code: str,
    description: str,
    parameters: dict | None = None,
    author_agent: str = "unknown",
    ticker_origin: str = "",
    backtest_results: dict | None = None,
) -> dict:
    """Save a new equation to the shared library.

    Returns the saved equation record or an error dict.
    """
    if len(code) > MAX_CODE_LENGTH:
        return {"error": f"Code exceeds max length ({MAX_CODE_LENGTH} chars)"}

    # Basic safety check — block dangerous operations
    blocked_keywords = [
        "import os", "import sys", "import subprocess", "import shutil",
        "__import__", "eval(", "exec(", "open(", "compile(",
        "getattr(", "setattr(", "delattr(", "globals(", "locals(",
        "breakpoint(", "__class__", "__subclasses__",
    ]
    code_lower = code.lower()
    for blocked in blocked_keywords:
        if blocked.lower() in code_lower:
            return {"error": f"Code contains blocked operation: {blocked}"}

    eq_id = f"eq-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)

    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO quant_equation_library
                (id, name, description, code, parameters, author_agent,
                 ticker_origin, backtest_results, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    code = EXCLUDED.code,
                    description = EXCLUDED.description,
                    parameters = EXCLUDED.parameters,
                    backtest_results = EXCLUDED.backtest_results,
                    updated_at = EXCLUDED.updated_at
                """,
                [
                    eq_id, name, description, code,
                    json.dumps(parameters or {}),
                    author_agent, ticker_origin,
                    json.dumps(backtest_results or {}),
                    now, now,
                ],
            )
        logger.info("[EQ_LIBRARY] Saved equation '%s' by %s", name, author_agent)
        return {
            "status": "saved",
            "id": eq_id,
            "name": name,
            "description": description,
        }
    except Exception as e:
        logger.error("[EQ_LIBRARY] Failed to save equation '%s': %s", name, e)
        return {"error": str(e)}


def search_equations(query: str = "", top_k: int = 10) -> list[dict]:
    """Search the equation library by keyword in name/description.

    Returns top_k equations sorted by win_rate descending.
    """
    try:
        with get_db() as db:
            if query:
                rows = db.execute(
                    """
                    SELECT id, name, description, code, parameters,
                           author_agent, ticker_origin, backtest_results,
                           usage_count, avg_pnl_pct, win_rate_pct, sharpe_ratio,
                           created_at
                    FROM quant_equation_library
                    WHERE name ILIKE %s OR description ILIKE %s
                    ORDER BY win_rate_pct DESC, usage_count DESC
                    LIMIT %s
                    """,
                    [f"%{query}%", f"%{query}%", top_k],
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT id, name, description, code, parameters,
                           author_agent, ticker_origin, backtest_results,
                           usage_count, avg_pnl_pct, win_rate_pct, sharpe_ratio,
                           created_at
                    FROM quant_equation_library
                    ORDER BY win_rate_pct DESC, usage_count DESC
                    LIMIT %s
                    """,
                    [top_k],
                ).fetchall()

        results = []
        for row in rows:
            results.append({
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "code": row[3],
                "parameters": row[4] if isinstance(row[4], dict) else json.loads(row[4] or "{}"),
                "author_agent": row[5],
                "ticker_origin": row[6],
                "backtest_results": row[7] if isinstance(row[7], dict) else json.loads(row[7] or "{}"),
                "usage_count": row[8],
                "avg_pnl_pct": row[9],
                "win_rate_pct": row[10],
                "sharpe_ratio": row[11],
                "created_at": str(row[12]),
            })

        logger.info("[EQ_LIBRARY] Search '%s' returned %d results", query, len(results))
        return results
    except Exception as e:
        logger.error("[EQ_LIBRARY] Search failed: %s", e)
        return []


def get_equation_by_name(name: str) -> dict | None:
    """Fetch a single equation by exact name."""
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT id, name, description, code, parameters FROM quant_equation_library WHERE name = %s",
                [name],
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0], "name": row[1], "description": row[2],
                "code": row[3], "parameters": row[4],
            }
    except Exception as e:
        logger.error("[EQ_LIBRARY] get_equation_by_name failed: %s", e)
        return None


def increment_usage(name: str) -> None:
    """Bump usage_count for an equation."""
    try:
        with get_db() as db:
            db.execute(
                "UPDATE quant_equation_library SET usage_count = usage_count + 1, "
                "updated_at = %s WHERE name = %s",
                [datetime.now(timezone.utc), name],
            )
    except Exception as e:
        logger.debug("[EQ_LIBRARY] increment_usage failed (non-fatal): %s", e)


def update_backtest_stats(
    name: str,
    pnl_pct: float,
    win_rate: float,
    sharpe: float,
    backtest_results: dict,
) -> None:
    """Update performance stats for an equation after a backtest run."""
    try:
        with get_db() as db:
            db.execute(
                """
                UPDATE quant_equation_library
                SET avg_pnl_pct = %s, win_rate_pct = %s, sharpe_ratio = %s,
                    backtest_results = %s, updated_at = %s
                WHERE name = %s
                """,
                [pnl_pct, win_rate, sharpe, json.dumps(backtest_results),
                 datetime.now(timezone.utc), name],
            )
    except Exception as e:
        logger.error("[EQ_LIBRARY] update_backtest_stats failed: %s", e)


# ── Sandboxed Executor ──────────────────────────────────────────────

def execute_equation(
    code: str,
    ticker: str,
    parameters: dict | None = None,
) -> dict:
    """Execute a Python equation in a restricted sandbox.

    The code receives:
      - `df`: A pandas DataFrame with price_history + technicals for the ticker
      - `params`: A dict of user-supplied parameters
      - `np`, `pd`: numpy and pandas modules

    The code MUST assign its result to a variable called `result`.

    Returns:
        {"status": "ok", "result": <value>} or {"error": "..."}
    """
    # Load data for the ticker
    try:
        from app.trading.quant_edge_verifier import load_historical_data
        df = load_historical_data(ticker)
        if df.empty:
            return {"error": f"No historical data available for {ticker}"}
    except Exception as e:
        return {"error": f"Failed to load data for {ticker}: {e}"}

    # Build sandbox namespace
    sandbox = dict(SAFE_GLOBALS)
    sandbox["df"] = df
    sandbox["params"] = parameters or {}
    sandbox["result"] = None

    # Capture stdout
    stdout_capture = io.StringIO()

    try:
        with contextlib.redirect_stdout(stdout_capture):
            exec(compile(code, "<equation>", "exec"), sandbox)  # noqa: S102

        result = sandbox.get("result")
        if result is None:
            return {"error": "Equation did not assign to 'result'. Your code must set result = ..."}

        # Convert numpy/pandas types to JSON-serializable
        if isinstance(result, (np.integer,)):
            result = int(result)
        elif isinstance(result, (np.floating,)):
            result = float(result)
        elif isinstance(result, np.ndarray):
            result = result.tolist()
        elif isinstance(result, pd.DataFrame):
            result = result.to_dict(orient="records")
        elif isinstance(result, pd.Series):
            result = result.to_dict()

        output = stdout_capture.getvalue().strip()
        resp = {"status": "ok", "result": result}
        if output:
            resp["stdout"] = output[:2000]
        return resp

    except Exception as e:
        return {"error": f"Equation execution failed: {type(e).__name__}: {e}"}

