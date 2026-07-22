"""
Precomputed quant-math context block.

The 2026-07-21 research audit found the quant analyst averages 1.6 loops of
its 14-loop budget and the board averages 1.0 — prompts telling them to CALL
the portfolio-math tools mostly don't fire. So the pipeline computes the math
in code during desk build and injects the results into their prompts instead;
the tools remain available for ad-hoc deeper dives.

Everything here is fail-open: any exception degrades to a missing line or an
empty block, never a pipeline error.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Below this many tickers there is no portfolio to diversify against.
_MIN_PORTFOLIO = 2


def build_quant_math_block(ticker: str, bot_id: str = "") -> str:
    """Code-computed GARCH + HRP/covariance + strategy-health lines for the
    ticker being analyzed. Returns "" when nothing could be computed."""
    ticker = ticker.strip().upper()
    parts: list[str] = []

    # ── GARCH(1,1) forward vol ──
    try:
        from app.quant.garch import garch_forecast
        from app.quant.returns import load_close_returns

        returns = load_close_returns(ticker, 500)
        if returns.size:
            g = garch_forecast(returns)
            if "error" not in g:
                parts.append(
                    f"- GARCH(1,1) next-day vol forecast: predicted "
                    f"{g['predicted_vol_annualized_pct']}% annualized vs realized "
                    f"{g['realized_vol_annualized_pct']}% (20d) — prediction premium "
                    f"{g['prediction_premium']:+.2f} → **{g['vol_signal']}**"
                )
            else:
                parts.append(f"- GARCH forecast unavailable: {g['error']}")
    except Exception as e:
        logger.debug("[QuantMathBlock] %s: GARCH failed (non-fatal): %s", ticker, e)

    # ── HRP allocation with this ticker as candidate ──
    try:
        from app.quant import portfolio_math
        from app.quant.returns import load_returns_matrix
        from app.tools.portfolio_tools import _current_holdings

        held_values, _cash, equity = _current_holdings(bot_id)
        universe = sorted(set(held_values) | {ticker})
        if len(universe) >= _MIN_PORTFOLIO:
            returns_df, dropped = load_returns_matrix(universe, 252)
            kept = list(returns_df.columns)
            if len(kept) >= _MIN_PORTFOLIO and ticker in kept:
                cov, _intensity = portfolio_math.ledoit_wolf_shrinkage(
                    returns_df.fillna(0.0).values
                )
                weights = portfolio_math.hrp_weights(cov)
                w_map = dict(zip(kept, weights))
                dr = portfolio_math.diversification_ratio(weights, cov)
                cond = portfolio_math.condition_number(cov)
                w_t = w_map[ticker]
                parts.append(
                    f"- HRP covariance-aware sizing (holdings + {ticker}): target "
                    f"weight for {ticker} = {w_t * 100:.1f}% of equity "
                    f"(≈${w_t * equity:,.0f}); portfolio diversification ratio "
                    f"{dr:.2f}; covariance condition {cond:.0f} "
                    f"({'HIGH — estimates unstable' if cond > 1000 else 'OK'})"
                )
                held_total = sum(held_values.values())
                if held_total > 0:
                    current = {t: held_values.get(t, 0.0) / held_total for t in kept}
                    drift = portfolio_math.rebalance_drift(current, w_map, 0.05)
                    if drift["breaches"]:
                        breach_txt = ", ".join(
                            f"{t} {d:+.0%}" for t, d in
                            sorted(drift["breaches"].items(), key=lambda x: -abs(x[1]))[:4]
                        )
                        parts.append(f"- Rebalance drift >5% vs HRP targets: {breach_txt}")
                if dropped:
                    parts.append(f"- (excluded from covariance, thin history: {', '.join(dropped[:5])})")
    except Exception as e:
        logger.debug("[QuantMathBlock] %s: HRP failed (non-fatal): %s", ticker, e)

    # ── Strategy health (only when it says something) ──
    try:
        from app.quant.strategy_health import get_pipeline_health

        health = get_pipeline_health()
        if health.get("status") in ("REDUCE", "CUT"):
            parts.append(
                f"- Strategy health: **{health['status']}** "
                f"({health.get('driver')}: {health.get('reason')}) — "
                f"{'new BUYs are policy-blocked' if health['status'] == 'CUT' else 'BUY sizes are halved by the pipeline'}"
            )
    except Exception as e:
        logger.debug("[QuantMathBlock] %s: health failed (non-fatal): %s", ticker, e)

    if not parts:
        return ""
    return (
        "## PRECOMPUTED QUANT MATH (computed in code this cycle — cite these "
        "numbers directly; tools only for deeper dives)\n" + "\n".join(parts)
    )
