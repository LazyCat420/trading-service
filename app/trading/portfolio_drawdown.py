"""
Portfolio Drawdown — computes max drawdown from realized trade history.

Used by the strategy auditor to report a real drawdown figure instead
of "Unknown".
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def compute_portfolio_drawdown(db, initial_cash: float = 100_000.0) -> Optional[float]:
    """Compute max drawdown from the closed-trade PnL series.

    Builds a cumulative equity curve from trade-level PnL ordered by
    close time, then computes the standard peak-to-trough drawdown.

    Returns:
        A negative float (e.g. -0.182 for -18.2%) or None if there
        are no closed trades.
    """
    from app.config import settings

    # settings.BOT_ID pointed at a bot with ZERO lot_closures while the active
    # bot had 6 (2026-07-24 audit), so this query returned no rows and the
    # function returned None — the drawdown breaker could never trip. A risk
    # control that silently reads the wrong book is worse than none, because it
    # reports "no drawdown" rather than "unknown".
    from app.tools.portfolio_tools import resolve_bot_id

    rows = db.execute(
        """
        SELECT realized_pnl
        FROM lot_closures
        WHERE bot_id = %s
        ORDER BY closed_at ASC
        """,
        [resolve_bot_id()]
    ).fetchall()

    if not rows:
        return None

    equity = initial_cash
    peak = equity
    max_dd = 0.0

    for (pnl,) in rows:
        equity += float(pnl)
        if equity > peak:
            peak = equity
        dd = (equity - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd

    return max_dd
