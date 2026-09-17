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
