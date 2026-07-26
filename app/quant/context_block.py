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
import time

logger = logging.getLogger(__name__)

# Below this many tickers there is no portfolio to diversify against.
_MIN_PORTFOLIO = 2

# Per-component wall-clock budget, in seconds.
#
# THE TRAP THIS EXISTS TO CLOSE (2026-07-25): every component below is
# fail-open individually, but they all ran under ONE outer timeout in the
# orchestrator. Adding the ~32s HMM shadow therefore blew the budget for the
# whole block and silently dropped GARCH, HRP *and* the sizing bracket —
# none of which had anything to do with the new code. All three desks in
# cycle-v3-1784960316 logged "quant math precompute failed (non-fatal): "
# with an EMPTY message (that is how asyncio.TimeoutError stringifies).
#
# Raising the outer timeout 25s -> 60s made that less likely; it did not make
# it impossible. Fail-open composition is not free: a slow item silently
# removes the fast ones already in the block. A per-component deadline means
# the block degrades to "the HMM line is missing" rather than "everything is
# missing", which is the difference between a visible gap and a silent one.
_COMPONENT_BUDGET_SEC = 45.0


def _with_deadline(fn, *, seconds: float, label: str, ticker: str):
    """Run `fn` on a worker thread and give up waiting after `seconds`.

    Returns None on timeout. The worker is NOT killed — Python cannot safely
    interrupt arbitrary CPU-bound code — so the thread is left as a daemon to
    finish and be discarded. That is acceptable here precisely because every
    component is pure computation over already-fetched data with no side
    effects: the cost of an abandoned fit is wasted CPU, not a corrupt write.

    What this buys is the thing the outer timeout could not: ONE slow
    component yields its own slot instead of consuming the whole block's.
    """
    import concurrent.futures as _f

    pool = _f.ThreadPoolExecutor(max_workers=1, thread_name_prefix="quantblk")
    try:
        future = pool.submit(fn)
        try:
            return future.result(timeout=seconds)
        except _f.TimeoutError:
            logger.warning(
                "[QuantMathBlock] %s: %s exceeded its %.1fs slot — that "
                "section is MISSING; the rest of the block survived",
                ticker, label, seconds,
            )
            return None
    finally:
        # Do not block on an overrunning worker.
        pool.shutdown(wait=False)


def _budget_exhausted(started: float, label: str, ticker: str) -> bool:
    """True when the block has spent its budget. Named so the log says which
    component was skipped, rather than leaving an empty exception message."""
    spent = time.monotonic() - started
    if spent < _COMPONENT_BUDGET_SEC:
        return False
    logger.warning(
        "[QuantMathBlock] %s: budget spent (%.1fs) before %s — that section is "
        "MISSING from this desk; the earlier sections survived",
        ticker, spent, label,
    )
    return True


def build_quant_math_block(
    ticker: str, bot_id: str = "", cycle_id: str | None = None,
) -> str:
    """Code-computed GARCH + HRP/covariance + strategy-health lines for the
    ticker being analyzed. Returns "" when nothing could be computed.

    `cycle_id` scopes the HMM regime cache to this cycle. Without it the cache
    falls back to calendar-date keying, so the first cycle of the day's fit is
    served to every later cycle (2026-07-25 audit).
    """
    ticker = ticker.strip().upper()
    parts: list[str] = []
    # Components are ordered cheapest-first below, so if the budget does run
    # out it is the expensive tail that is lost, not the whole block.
    _started = time.monotonic()

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
                # UNITS (fixed 2026-07-25): HRP weights sum to 1.0 across the
                # INVESTED universe, so this is a fraction of invested capital,
                # not of equity. Calling it "% of equity" overstated it by
                # ~2x on a 47%-cash book — and the board read one such line
                # literally, sizing VZ at 19.2%. State the basis explicitly and
                # label it a target weight, never an order size.
                _invested = sum(held_values.values()) or equity
                parts.append(
                    f"- HRP covariance-aware target weight for {ticker} = "
                    f"{w_t * 100:.1f}% of INVESTED capital "
                    f"(≈${w_t * _invested:,.0f}; the book is "
                    f"{(equity - _invested) / equity * 100:.0f}% cash). This is a "
                    f"portfolio target weight, NOT an order size — see the "
                    f"SIZING BRACKET for what may actually be bought. "
                    f"Diversification ratio {dr:.2f}; covariance condition "
                    f"{cond:.0f} "
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

    # ── Cross-sectional factor exposures (2026-07-25) ──
    # Only price-derived factors: `fundamentals` holds 76 snapshot dates from
    # 2026-05-06, so value/profitability/investment cannot be built without
    # look-ahead bias. See app/quant/factors.py.
    try:
        from app.quant import factors as factor_lib
        from app.tools.portfolio_tools import _current_holdings

        held_values, _c, _e = _current_holdings(bot_id)
        universe = sorted(set(held_values) | {ticker})
        if len(universe) >= factor_lib.MIN_CROSS_SECTION:
            exposures = factor_lib.factor_exposures_for(ticker, universe)
            if exposures:
                rendered = ", ".join(
                    f"{name} {z:+.2f}σ" for name, z in sorted(exposures.items())
                )
                parts.append(
                    f"- Factor exposures for {ticker} (cross-sectional z-scores vs the "
                    f"{len(universe)}-name book): {rendered}. Positive momentum = strong "
                    f"12-1 trend; positive low_vol = CALMER than peers; positive reversal "
                    f"= recent loser (bounce candidate). Price-derived only."
                )
    except Exception as e:
        logger.debug("[QuantMathBlock] %s: factor exposures failed (non-fatal): %s", ticker, e)

    # ── HMM regime shadow (2026-07-25) ──
    # A price-only regime posterior that is emitted EVERY cycle, unlike the
    # regime engine's forward_call (scoreable in 7 of 130 desks). Shadow only:
    # it never overrides the Regime Engine. See app/quant/regime_hmm.py.
    # The ~32s first-call cost makes this the component most likely to eat the
    # block's whole budget, so it is the one that must yield first.
    if not _budget_exhausted(_started, "HMM regime shadow", ticker):
        try:
            from app.quant.regime_hmm import build_hmm_context_line

            # Hard deadline on the CALL, not merely a check before it: a
            # pre-call budget check cannot stop a component that starts just
            # under budget and then hangs. Without this the outer timeout is
            # still the only backstop, and it takes the neighbours with it.
            hmm_line = _with_deadline(
                lambda: build_hmm_context_line(cycle_id=cycle_id),
                seconds=max(1.0, _COMPONENT_BUDGET_SEC - (time.monotonic() - _started)),
                label="HMM regime shadow", ticker=ticker,
            )
            if hmm_line:
                parts.append(hmm_line)
        except Exception as e:
            logger.debug("[QuantMathBlock] %s: HMM shadow failed (non-fatal): %s", ticker, e)

    # ── Sizing bracket (2026-07-25) ──
    # Appended as its own block rather than another bullet: sizing needs the
    # units stated and the binding constraint named, which is exactly what a
    # one-line bullet loses. See app/quant/sizing_bracket.py for why.
    bracket = ""
    if not _budget_exhausted(_started, "sizing bracket", ticker):
        try:
            from app.quant.sizing_bracket import build_sizing_bracket

            bracket = build_sizing_bracket(ticker, bot_id) or ""
        except Exception as e:
            logger.debug("[QuantMathBlock] %s: sizing bracket failed: %s", ticker, e)

    if not parts and not bracket:
        return ""
    block = ""
    if parts:
        block = (
            "## PRECOMPUTED QUANT MATH (computed in code this cycle — cite these "
            "numbers directly; tools only for deeper dives)\n" + "\n".join(parts)
        )
    if bracket:
        block = (block + "\n\n" + bracket) if block else bracket
    return block
