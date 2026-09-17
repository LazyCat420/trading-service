"""Separated Evaluation Engine for Decision Alpha, Execution Drag, and Net Alpha.

Separates thesis measurement from execution friction:
- Decision Alpha = Decision Return - Benchmark Return
- Execution Drag = Executed Return - Comparable Decision Return
- Net Alpha = Executed Return - Benchmark Return
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def compute_return(p_entry: float, p_exit: float, side: str = "BUY") -> float:
    if p_entry <= 0:
        return 0.0
    if side.upper() == "SELL":
        return round(((p_entry - p_exit) / p_entry) * 100.0, 4)
    return round(((p_exit - p_entry) / p_entry) * 100.0, 4)


class EvaluationMetrics:
    def __init__(
        self,
        decision_return: Optional[float] = None,
        benchmark_return: Optional[float] = None,
        decision_alpha: Optional[float] = None,
        executed_return: Optional[float] = None,
        execution_drag: Optional[float] = None,
        net_alpha: Optional[float] = None,
    ):
        self.decision_return = decision_return
        self.benchmark_return = benchmark_return
        self.decision_alpha = decision_alpha
        self.executed_return = executed_return
        self.execution_drag = execution_drag
        self.net_alpha = net_alpha

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_return": self.decision_return,
            "benchmark_return": self.benchmark_return,
            "decision_alpha": self.decision_alpha,
            "executed_return": self.executed_return,
            "execution_drag": self.execution_drag,
            "net_alpha": self.net_alpha,
        }


class DecisionEvaluator:
    """Evaluates the underlying directional thesis independent of execution fills."""

    @staticmethod
    def evaluate(
        entry_price: float,
        horizon_price: float,
        benchmark_entry: float,
        benchmark_horizon: float,
        action: str = "BUY",
    ) -> EvaluationMetrics:
        dec_ret = compute_return(entry_price, horizon_price, side=action)
        bm_ret = compute_return(benchmark_entry, benchmark_horizon, side="BUY")
        dec_alpha = round(dec_ret - bm_ret, 4)

        return EvaluationMetrics(
            decision_return=dec_ret,
            benchmark_return=bm_ret,
            decision_alpha=dec_alpha,
        )


class ExecutionEvaluator:
    """Evaluates implementation quality: fill prices, fees, slippage, and position path."""

    @staticmethod
    def evaluate(
        fill_price: float,
        exit_realized_price: float,
        reference_price: float,
        benchmark_entry: float,
        benchmark_horizon: float,
        fees: float = 0.0,
        fill_value: float = 1.0,
        action: str = "BUY",
    ) -> EvaluationMetrics:
        # Net executed return accounting for fee friction
        raw_ret = compute_return(fill_price, exit_realized_price, side=action)
        fee_pct = (fees / max(fill_value, 1.0)) * 100.0
        exec_ret = round(raw_ret - fee_pct, 4)

        # Comparable decision return using reference price
        comp_dec_ret = compute_return(reference_price, exit_realized_price, side=action)

        exec_drag = round(exec_ret - comp_dec_ret, 4)
        bm_ret = compute_return(benchmark_entry, benchmark_horizon, side="BUY")
        net_alpha = round(exec_ret - bm_ret, 4)

        return EvaluationMetrics(
            decision_return=comp_dec_ret,
            benchmark_return=bm_ret,
            decision_alpha=round(comp_dec_ret - bm_ret, 4),
            executed_return=exec_ret,
            execution_drag=exec_drag,
            net_alpha=net_alpha,
        )


class LotAlphaEvaluator:
    """Evaluates realized FIFO lot closures and separates policy effect from execution drag."""

    @staticmethod
    def evaluate_lot_closure(
        lot_entry_price: float,
        lot_exit_price: float,
        benchmark_entry: Optional[float],
        benchmark_exit: Optional[float],
        fees: float = 0.0,
        notional: float = 1.0,
        is_closing_long: bool = True,
        qty: Optional[float] = None,
        allocated_entry_fee: float = 0.0,
        exit_fee: float = 0.0,
        fees_embedded_in_fills: bool = False,
    ) -> dict[str, Any]:
        """Evaluates a closed lot.
        
        Guarantees:
        - Reconciles cash flow dollar P&L and invested-capital denominator.
        - Apportions entry and exit fees consistently without double deduction.
        - Follows contract formula: net_return = dollar_pnl / invested_capital.
        """
        if lot_entry_price <= 0:
            return {"status": "INVALID", "reason": "Zero or negative entry price"}

        # Resolve quantity
        if qty is not None and qty > 0.0:
            resolved_qty = float(qty)
        elif notional > 0.0 and lot_exit_price > 0.0:
            resolved_qty = notional / lot_exit_price
        else:
            resolved_qty = 1.0

        # Resolve fees
        entry_f = float(allocated_entry_fee or 0.0)
        exit_f = float(exit_fee or 0.0)
        if entry_f == 0.0 and exit_f == 0.0 and fees > 0.0:
            exit_f = float(fees)

        # Calculate cash flows and invested capital
        gross_pnl = resolved_qty * (lot_exit_price - lot_entry_price) if is_closing_long else resolved_qty * (lot_entry_price - lot_exit_price)
        
        if fees_embedded_in_fills:
            invested_capital = lot_entry_price * resolved_qty
            dollar_pnl = gross_pnl
            total_fees = 0.0
            entry_f = 0.0
            exit_f = 0.0
        else:
            invested_capital = (lot_entry_price * resolved_qty) + entry_f
            dollar_pnl = gross_pnl - entry_f - exit_f
            total_fees = entry_f + exit_f

        invested_capital = max(invested_capital, 1e-6)
        net_return = round((dollar_pnl / invested_capital) * 100.0, 4)
        
        gross_return = round(
            (((lot_exit_price - lot_entry_price) / lot_entry_price) * 100.0)
            if is_closing_long
            else (((lot_entry_price - lot_exit_price) / lot_entry_price) * 100.0),
            4,
        )
        fee_drag_pct = round(gross_return - net_return, 4)

        base_res: dict[str, Any] = {
            "invested_capital_denominator": round(invested_capital, 4),
            "dollar_pnl": round(dollar_pnl, 4),
            "gross_pnl": round(gross_pnl, 4),
            "allocated_entry_fee": round(entry_f, 4),
            "exit_fee": round(exit_f, 4),
            "total_fees": round(total_fees, 4),
            "gross_return": gross_return,
            "fee_drag_pct": fee_drag_pct,
            "net_return": net_return,
        }

        if not benchmark_entry or not benchmark_exit or benchmark_entry <= 0:
            base_res.update({
                "status": "UNRESOLVED",
                "benchmark_return": None,
                "net_alpha": None,
                "reason": "MISSING_SOURCE_PINNED_BENCHMARK",
            })
            return base_res

        bm_return = round(((benchmark_exit - benchmark_entry) / benchmark_entry) * 100.0, 4)
        net_alpha = round(net_return - bm_return, 4)

        base_res.update({
            "status": "MATURE",
            "benchmark_return": bm_return,
            "net_alpha": net_alpha,
        })
        return base_res
