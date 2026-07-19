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

# Round-trip friction: 7.5 bps per side (commission + slippage). Cost-free
# backtests systematically promote high-churn strategies whose paper edge
# dies on the first real spread.
COST_PCT_PER_SIDE = 0.075  # percent of price, per side

# Below this many closed trades a backtest is statistical noise, not evidence.
MIN_TRADES_FOR_GATE = 5

# Fraction of trades (chronological) used as in-sample; the tail is held out.
IS_FRACTION = 0.7

# The OOS edge must beat this share of sign-randomized (coin-flip) resamples.
NULL_PERCENTILE_GATE = 0.60
_NULL_RESAMPLES = 200


def _sharpe_from_trades(trades: list[dict]) -> float:
    """Sharpe from per-trade returns, annualized by ACTUAL holding period.

    The old formula annualized per-trade returns with sqrt(252) — treating a
    9-day swing trade as if it were a daily return — inflating multi-day
    strategies' Sharpe by ~3x. Annualize by 252/avg_holding_days instead.
    """
    if not trades or len(trades) < 2:
        return 0.0
    returns = [t.get("return_pct", 0.0) for t in trades]
    std_r = np.std(returns, ddof=1)
    if std_r <= 0:
        return 0.0
    hold_days = []
    for t in trades:
        try:
            d = (pd.to_datetime(t["exit_date"]) - pd.to_datetime(t["entry_date"])).days
            hold_days.append(max(int(d), 1))
        except Exception:
            pass
    avg_hold = float(np.mean(hold_days)) if hold_days else 1.0
    return float((np.mean(returns) / std_r) * np.sqrt(252.0 / max(avg_hold, 1.0)))


def _null_percentile(trades: list[dict]) -> float | None:
    """Share of sign-randomized resamples the actual edge beats.

    Flips each trade's direction with p=0.5 (a coin-flip trader taking the
    same entries/exits) and asks how often the real cumulative return beats
    the coin flipper. ~0.5 = indistinguishable from luck; deterministic seed
    keeps repeat runs stable.
    """
    if not trades or len(trades) < MIN_TRADES_FOR_GATE:
        return None
    returns = np.array([t.get("return_pct", 0.0) for t in trades]) / 100.0
    actual = float(np.prod(1.0 + returns) - 1.0)
    rng = np.random.default_rng(seed=len(trades) * 1000 + int(abs(actual) * 1e6) % 997)
    beats = 0
    for _ in range(_NULL_RESAMPLES):
        signs = rng.choice([1.0, -1.0], size=len(returns))
        null_cum = float(np.prod(1.0 + returns * signs) - 1.0)
        if actual > null_cum:
            beats += 1
    return round(beats / _NULL_RESAMPLES, 3)


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

    # An equation that RETURNS a ready-made summary is self-reporting — the
    # code could fabricate any numbers it likes (adversarial test proved a
    # 99%-PnL liar equation sailed through the gate and into library stats).
    # Only summaries derived from OUR trade simulation are trusted; label
    # self-reports and never write their stats.
    if isinstance(raw_result, dict) and "total_trades" in raw_result and "signals" not in raw_result:
        out = dict(raw_result)
        out["self_reported"] = True
        out["note"] = "equation returned its own summary — not simulation-verified; treated as unbacktested"
        logger.warning("[BACKTEST] '%s' returned a SELF-REPORTED summary — not trusted for gating", equation_name)
        return out

    # If the equation returns signals, simulate trades (net of costs), then
    # split chronologically: the OOS tail + a coin-flip null test are what the
    # Stage-2 gate actually judges — full-sample stats alone reward overfit.
    if isinstance(raw_result, dict) and "signals" in raw_result:
        signals = raw_result["signals"]
        trades = _simulate_from_signals(signals)
        summary = _summarize_trades(trades)
        split = max(1, int(len(trades) * IS_FRACTION)) if trades else 0
        is_trades, oos_trades = trades[:split], trades[split:]
        summary["costs_applied_pct_per_side"] = COST_PCT_PER_SIDE
        summary["sharpe_ratio"] = _sharpe_from_trades(trades)
        summary["in_sample"] = _summarize_trades(is_trades) if is_trades else {}
        summary["out_of_sample"] = _summarize_trades(oos_trades) if oos_trades else {}
        summary["oos_trades"] = len(oos_trades)
        summary["null_percentile"] = _null_percentile(oos_trades if len(oos_trades) >= MIN_TRADES_FOR_GATE else trades)
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
                # Net of friction: pay COST_PCT_PER_SIDE on entry AND exit.
                eff_entry = entry_price * (1 + COST_PCT_PER_SIDE / 100.0)
                eff_exit = price * (1 - COST_PCT_PER_SIDE / 100.0)
                pnl = (eff_exit - eff_entry) / eff_entry
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
        sharpe = summary.get("sharpe_ratio")
        if not isinstance(sharpe, (int, float)) or not sharpe:
            sharpe = _sharpe_from_trades(summary.get("trades", []))

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

        if result.get("self_reported"):
            pitch["backtest_results"] = {"note": result.get("note", "self-reported summary — not simulation-verified")}
            pitch["backtest_pnl"] = None
            pitch["backtest_sharpe"] = None
            survivors.append(pitch)
            logger.info("[BACKTEST] '%s' passed through UNVERIFIED (self-reported summary)", eq_name)
            continue

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
        sharpe = result.get("sharpe_ratio", 0.0) or _sharpe_from_trades(result.get("trades", []))
        n_trades = int(result.get("total_trades") or len(result.get("trades", []) or []))

        # Too few closed trades = no statistical evidence either way — pass
        # through honestly unbacktested (like stub equations) rather than
        # pretending a 2-trade "backtest" proved anything.
        if n_trades < MIN_TRADES_FOR_GATE:
            pitch["backtest_results"] = {"note": f"only {n_trades} trades — below evidence threshold ({MIN_TRADES_FOR_GATE})"}
            pitch["backtest_pnl"] = None
            pitch["backtest_sharpe"] = None
            survivors.append(pitch)
            logger.info("[BACKTEST] '%s' passed through unbacktested (%d trades < %d)",
                        eq_name, n_trades, MIN_TRADES_FOR_GATE)
            continue

        # Judge the OUT-OF-SAMPLE tail when it has enough trades; the
        # full-sample number is what the strategy was (implicitly) fit on.
        oos = result.get("out_of_sample") or {}
        gate_pnl = oos.get("cumulative_return_pct", pnl) if int(result.get("oos_trades") or 0) >= MIN_TRADES_FOR_GATE else pnl
        null_pct = result.get("null_percentile")

        if gate_pnl < min_pnl:
            logger.info(
                "[BACKTEST] Eliminated '%s': gated PnL %.2f%% < %.2f%% (net of costs, OOS-preferred)",
                eq_name, gate_pnl, min_pnl,
            )
            continue

        if isinstance(null_pct, (int, float)) and null_pct < NULL_PERCENTILE_GATE:
            logger.info(
                "[BACKTEST] Eliminated '%s': beats only %.0f%% of coin-flip resamples (< %.0f%%)",
                eq_name, null_pct * 100, NULL_PERCENTILE_GATE * 100,
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
            "[BACKTEST] '%s' survived: PnL=%.2f%% (gated %.2f%%), Sharpe=%.2f, null_pct=%s",
            eq_name, pnl, gate_pnl, sharpe, null_pct,
        )

    logger.info(
        "[BACKTEST] Filter complete: %d/%d pitches survived",
        len(survivors), len(pitches),
    )
    return survivors
