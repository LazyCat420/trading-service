"""
Backtest Runner — Deterministic equation backtesting for Tournament Stage 2.

Takes a pitched equation from the Equation Library and runs it against
historical price data. Returns standardized performance metrics used
to eliminate losing strategies before the debate bracket.
"""

import logging
import json
import numpy as np
import pandas as pd

from app.cognition.debate.equation_library import (
    execute_equation,
    get_equation_by_name,
    update_backtest_stats,
    increment_usage,
)
from app.trading.quant_edge_verifier import (
    load_historical_data,
    _summarize_trades,
)

logger = logging.getLogger(__name__)


def run_backtest_for_equation(
    equation_name: str,
    ticker: str,
    parameters: dict | None = None,
) -> dict:
    """Run a named equation from the library against historical data.

    The equation code must produce a `result` dict with at minimum:
      - "signals": list of {"date": str, "action": "BUY"|"SELL", "price": float}

    If the equation produces raw signals, this runner simulates trades
    and computes standardized performance metrics.

    Returns:
        A backtest summary dict with PnL, win rate, Sharpe, max drawdown.
    """
    eq = get_equation_by_name(equation_name)
    if not eq:
        return {"error": f"Equation '{equation_name}' not found in library"}

    increment_usage(equation_name)

    # Execute the equation
    exec_result = execute_equation(eq["code"], ticker, parameters)
    if "error" in exec_result:
        return exec_result

    raw_result = exec_result.get("result", {})

    # Equations saved from pitches without executable code declare themselves
    # unbacktestable — report that honestly instead of fabricating metrics.
    if isinstance(raw_result, dict) and raw_result.get("unbacktestable"):
        return {"unbacktestable": True, "equation": equation_name}

    # If the equation already returns a full backtest summary, use it
    if isinstance(raw_result, dict) and "total_trades" in raw_result:
        _update_stats(equation_name, raw_result)
        return raw_result

    # If the equation returns signals, simulate trades
    if isinstance(raw_result, dict) and "signals" in raw_result:
        signals = raw_result["signals"]
        trades = _simulate_from_signals(signals)
        summary = _summarize_trades(trades)
        _update_stats(equation_name, summary)
        return summary

    # If the equation returns a single score/metric
    if isinstance(raw_result, (int, float)):
        return {
            "status": "ok",
            "equation": equation_name,
            "ticker": ticker,
            "score": raw_result,
            "note": "Equation returned a single numeric score, not trade signals",
        }

    return {
        "status": "ok",
        "equation": equation_name,
        "ticker": ticker,
        "raw_result": raw_result,
        "note": "Equation returned non-standard output. Consider adding 'signals' key.",
    }


def _simulate_from_signals(signals: list[dict]) -> list[dict]:
    """Convert a list of BUY/SELL signals into simulated trades."""
    trades = []
    position = 0
    entry_price = 0.0
    entry_date = None

    for sig in signals:
        action = sig.get("action", "").upper()
        price = sig.get("price", 0.0)
        date = sig.get("date", "")

        if position == 0 and action == "BUY":
            position = 1
            entry_price = price
            entry_date = date
        elif position == 1 and action == "SELL":
            position = 0
            if entry_price > 0:
                pnl = (price - entry_price) / entry_price
                trades.append({
                    "entry_date": entry_date,
                    "exit_date": date,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "return_pct": pnl * 100,
                })

    return trades


def _update_stats(equation_name: str, summary: dict) -> None:
    """Update equation library stats after a backtest."""
    try:
        pnl = summary.get("cumulative_return_pct", 0.0)
        wr = summary.get("win_rate_pct", 0.0)

        # Compute Sharpe ratio from individual trade returns
        trades = summary.get("trades", [])
        if trades and len(trades) >= 2:
            returns = [t.get("return_pct", 0.0) for t in trades]
            mean_r = np.mean(returns)
            std_r = np.std(returns, ddof=1)
            sharpe = (mean_r / std_r) * np.sqrt(252) if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        update_backtest_stats(equation_name, pnl, wr, sharpe, summary)
    except Exception as e:
        logger.debug("[BACKTEST] Failed to update stats for %s: %s", equation_name, e)


def filter_pitches_by_backtest(
    pitches: list[dict],
    ticker: str,
    min_pnl: float = 0.0,
    min_sharpe: float = 0.0,
) -> list[dict]:
    """Stage 2 filter: Run backtests on all pitched equations, eliminate losers.

    Args:
        pitches: List of pitch dicts, each with at minimum "equation_name".
        ticker: The ticker to backtest against.
        min_pnl: Minimum cumulative PnL % to survive (default: 0 = positive).
        min_sharpe: Minimum Sharpe ratio to survive (default: 0).

    Returns:
        List of pitches that survived the backtest filter, with
        backtest_results appended to each.
    """
    survivors = []
    for pitch in pitches:
        eq_name = pitch.get("equation_name", "")
        if not eq_name:
            logger.warning("[BACKTEST] Pitch has no equation_name, skipping")
            continue

        params = pitch.get("parameters", {})
        result = run_backtest_for_equation(eq_name, ticker, params)

        if result.get("unbacktestable"):
            # No executable code — the thesis can't be PnL-gated, but
            # eliminating it on missing data would be worse than passing it
            # through with an honest "no backtest" marker (backtest_pnl=None;
            # seeding treats None as 0, the jury sees N/A).
            pitch["backtest_results"] = {"note": "no executable code — not backtested"}
            pitch["backtest_pnl"] = None
            pitch["backtest_sharpe"] = None
            survivors.append(pitch)
            logger.info("[BACKTEST] '%s' passed through unbacktested (no executable code)", eq_name)
            continue

        if "error" in result:
            logger.info(
                "[BACKTEST] Eliminated '%s': %s", eq_name, result["error"]
            )
            continue

        pnl = result.get("cumulative_return_pct", 0.0)
        sharpe = result.get("sharpe_ratio", 0.0)

        # Compute sharpe if not in result
        if sharpe == 0.0:
            trades = result.get("trades", [])
            if trades and len(trades) >= 2:
                returns = [t.get("return_pct", 0.0) for t in trades]
                mean_r = np.mean(returns)
                std_r = np.std(returns, ddof=1)
                sharpe = (mean_r / std_r) * np.sqrt(252) if std_r > 0 else 0.0

        if pnl < min_pnl:
            logger.info(
                "[BACKTEST] Eliminated '%s': PnL %.2f%% < %.2f%% threshold",
                eq_name, pnl, min_pnl,
            )
            continue

        if sharpe < min_sharpe:
            logger.info(
                "[BACKTEST] Eliminated '%s': Sharpe %.2f < %.2f threshold",
                eq_name, sharpe, min_sharpe,
            )
            continue

        pitch["backtest_results"] = result
        pitch["backtest_pnl"] = pnl
        pitch["backtest_sharpe"] = sharpe
        survivors.append(pitch)
        logger.info(
            "[BACKTEST] '%s' survived: PnL=%.2f%%, Sharpe=%.2f",
            eq_name, pnl, sharpe,
        )

    logger.info(
        "[BACKTEST] Filter complete: %d/%d pitches survived",
        len(survivors), len(pitches),
    )
    return survivors
