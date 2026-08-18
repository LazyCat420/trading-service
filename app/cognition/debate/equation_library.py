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
# LLM-authored equations almost always open with `import numpy as np` /
# `import pandas as pd` even though both are pre-injected; without an
# __import__ in the builtins every such equation dies with
# "ImportError: __import__ not found" before its first real line.
_ALLOWED_IMPORT_ROOTS = {"numpy", "pandas", "math", "statistics"}


def _safe_import(name, *args, **kwargs):
    root = name.partition(".")[0]
    if root not in _ALLOWED_IMPORT_ROOTS:
        raise ImportError(
            f"import of '{name}' is not allowed in the equation sandbox "
            f"(allowed: {sorted(_ALLOWED_IMPORT_ROOTS)})"
        )
    return __import__(name, *args, **kwargs)


# ── Statistical inference inside the sandbox ─────────────────────────
# scipy is NOT importable here (see _ALLOWED_IMPORT_ROOTS), so an equation
# could not compute a normal CDF, a t-stat, or a confidence interval — it had
# `np` and `pd` and nothing else. The measured consequence: agents reach for
# the generic escape hatch instead. Over 30 days `run_equation` took 18 calls
# and `execute_python` took 27 (100% success) across FOUR agents — quant,
# board, regime engine and decision synthesizer. The demand for computation is
# real and already expressed; what was missing was the statistics.
#
# `app/quant/stat_gates.py` imports only `math` + `numpy`, so every function
# below is injectable with NO new dependency and no change to the import
# allow-list or the security surface — they are pure functions over arrays.
#
# `deflated_sharpe_ratio` is the one that matters most here: an agent that
# invents equations is multiple-testing by construction, and the DSR is the
# correction for exactly that. An equation library without it is a machine for
# manufacturing overfit Sharpes.
#
# Every name added here MUST also be named in the run_equation/save_equation
# tool descriptions (app/tools/quant_tools.py) — those descriptions say what
# the sandbox provides, and a helper the description does not mention is a
# helper no model will ever call. Pinned by
# tests/unit/test_sandbox_stat_helpers.py.
from app.quant.stat_gates import (  # noqa: E402
    deflated_sharpe_ratio,
    full_gate,
    is_oos_degradation,
    min_track_record_length,
    newey_west_tstat,
    probabilistic_sharpe_ratio,
    stationary_bootstrap_ci,
    suggest_lag,
)
from app.quant.stat_gates import _norm_cdf as _norm_cdf_impl  # noqa: E402
from app.db import mongo_query, mongo_store

_STAT_HELPERS = {
    "newey_west_tstat": newey_west_tstat,
    "stationary_bootstrap_ci": stationary_bootstrap_ci,
    "probabilistic_sharpe_ratio": probabilistic_sharpe_ratio,
    "deflated_sharpe_ratio": deflated_sharpe_ratio,
    "min_track_record_length": min_track_record_length,
    "is_oos_degradation": is_oos_degradation,
    "full_gate": full_gate,
    "suggest_lag": suggest_lag,
    # Exposed without the underscore: an equation needs a normal CDF far more
    # often than it needs to know this is a private helper upstream.
    "norm_cdf": _norm_cdf_impl,
}


SAFE_GLOBALS = {
    "__builtins__": {
        # Math/logic
        "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
        "len": len, "range": range, "enumerate": enumerate, "zip": zip,
        "sorted": sorted, "reversed": reversed, "filter": filter, "map": map,
        "isinstance": isinstance, "float": float, "int": int, "str": str,
        "bool": bool, "list": list, "dict": dict, "tuple": tuple, "set": set,
        "True": True, "False": False, "None": None,
        # Introspection/iteration helpers LLM equations reach for constantly
        # (live cycles died on NameError: hasattr after the __import__ fix)
        "hasattr": hasattr, "getattr": getattr, "any": any, "all": all,
        "divmod": divmod, "pow": pow, "format": format, "repr": repr,
        "iter": iter, "next": next, "callable": callable, "slice": slice,
        "frozenset": frozenset, "type": type,
        "print": lambda *a, **kw: None,  # Silently swallow prints
        "ValueError": ValueError, "TypeError": TypeError,
        "KeyError": KeyError, "IndexError": IndexError,
        "Exception": Exception, "ZeroDivisionError": ZeroDivisionError,
        "AttributeError": AttributeError, "NameError": NameError,
        "StopIteration": StopIteration, "RuntimeError": RuntimeError,
        "ArithmeticError": ArithmeticError, "OverflowError": OverflowError,
        "__import__": _safe_import,
    },
    "np": np,
    "pd": pd,
    **_STAT_HELPERS,
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
            mongo_store.update_docs('quant_equation_library', {'name': name}, {'$set': {'code': code, 'description': description, 'parameters': json.dumps(parameters or {}), 'backtest_results': json.dumps(backtest_results or {}), 'updated_at': now}, '$setOnInsert': {'id': eq_id, 'author_agent': author_agent, 'ticker_origin': ticker_origin, 'created_at': now}}, upsert=True)
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
                rows = mongo_query.find_rows('quant_equation_library', {}, ['id', 'name', 'description', 'code', 'parameters', 'author_agent', 'ticker_origin', 'backtest_results', 'usage_count', 'avg_pnl_pct', 'win_rate_pct', 'sharpe_ratio', 'created_at'], sort=[('win_rate_pct', -1), ('usage_count', -1)], limit=top_k)

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
                "usage_count": row[8] or 0,
                # The 2026-07-16 fabricated-backtest purge NULLed these stats;
                # consumers format them (f"{x:.2f}") and crash on None — this
                # killed EVERY tournament pitch (0/4 → instant fallback).
                "avg_pnl_pct": row[9] if row[9] is not None else 0.0,
                "win_rate_pct": row[10] if row[10] is not None else 0.0,
                "sharpe_ratio": row[11] if row[11] is not None else 0.0,
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
            row = mongo_query.find_row('quant_equation_library', {'name': name}, ['id', 'name', 'description', 'code', 'parameters'])
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
            mongo_store.update_docs('quant_equation_library', {'name': name}, {'$set': {'avg_pnl_pct': pnl_pct, 'win_rate_pct': win_rate, 'sharpe_ratio': sharpe, 'backtest_results': json.dumps(backtest_results), 'updated_at': datetime.now(timezone.utc)}})
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
      - the statistics helpers in `_STAT_HELPERS` (Newey-West t-stat, stationary
        bootstrap CI, probabilistic/deflated Sharpe, min track record, OOS
        degradation, `full_gate`, `norm_cdf`). scipy is deliberately NOT
        importable, so these are the only route to a distribution or a test.

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

