"""Position-sizing bracket — the binding constraints, computed in code.

Across 182 BUYs the board used **11 distinct sizes** and 79% were exactly 3%,
4% or 5%. Sizing was a habit, not a calculation. The one time a size WAS
anchored to a computed number it went wrong in an instructive way: the board
read the HRP line "target weight for VZ = 19.2% of equity" and sized the order
at 19.2% — reading a *portfolio target weight* as a *single order size*. The
`MAX_POSITION_SIZE_PCT` cap caught it at 10%, but the cap catching a mistake is
not the same as the reasoning being right.

So this block does NOT hand the agent another raw number to copy. It computes
each binding constraint, states its units explicitly, and says **which one
binds** — the agent's job becomes judgment inside a bracket rather than
picking a habitual number or echoing whatever figure it last read.

Four constraints, all as a fraction of equity:

  risk-based   1% of equity at risk if price falls to the ATR stop
  HRP ceiling  covariance-aware target weight — a CEILING, not a size
  cash         what is actually available to spend
  concentration MAX_CONCENTRATION_PCT less what is already held

Everything here is fail-open: any exception degrades to a missing line or an
empty block, never a pipeline error.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Fraction of equity to risk on a single trade if the stop is hit. The
# textbook 1% rule; deliberately not a tunable parameter until the bracket has
# evidence behind it.
_RISK_PER_TRADE = 0.01


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def build_sizing_bracket(ticker: str, bot_id: str = "") -> str:
    """Return the injectable sizing-bracket block, or "" if nothing computed.

    Only meaningful for a potential BUY; callers inject it for every ticker
    because the board decides the action after reading it.
    """
    ticker = ticker.strip().upper()
    try:
        from app.services.parameter_store import get_param
        from app.tools.portfolio_tools import _current_holdings

        held_values, cash, equity = _current_holdings(bot_id)
        if not equity or equity <= 0:
            return ""

        constraints: dict[str, float] = {}
        notes: list[str] = []

        # ── 1. Risk-based size: 1% of equity at the ATR stop ──
        atr_pct = None
        try:
            from app.quant.technical_baseline import compute_technical_baseline

            b = compute_technical_baseline(ticker) or {}
            atr, close = b.get("atr"), b.get("close")
            if atr and close and float(close) > 0:
                # 2x ATR is the conventional stop distance.
                atr_pct = (2.0 * float(atr)) / float(close)
                if atr_pct > 0:
                    constraints["risk-based (1% equity at a 2xATR stop)"] = (
                        _RISK_PER_TRADE / atr_pct
                    )
                    notes.append(
                        f"2xATR stop is {_fmt_pct(atr_pct)} below spot"
                        + (" (STALE baseline)" if b.get("stale") else "")
                    )
        except Exception as e:
            logger.debug("[SizingBracket] %s: ATR leg failed: %s", ticker, e)

        # ── 2. HRP ceiling — a target WEIGHT, not an order size ──
        try:
            from app.quant import portfolio_math
            from app.quant.returns import load_returns_matrix

            universe = sorted(set(held_values) | {ticker})
            if len(universe) >= 2:
                returns_df, _dropped = load_returns_matrix(universe, 252)
                kept = list(returns_df.columns)
                if len(kept) >= 2 and ticker in kept:
                    cov, _ = portfolio_math.ledoit_wolf_shrinkage(
                        returns_df.fillna(0.0).values
                    )
                    weights = portfolio_math.hrp_weights(cov)
                    w_t = dict(zip(kept, weights))[ticker]
                    if w_t > 0:
                        # UNITS. HRP weights sum to 1.0 across the INVESTED
                        # universe, so w_t is a fraction of invested capital,
                        # NOT of equity. With a 47%-cash book those differ by
                        # ~2x. Converting here is the whole point of this
                        # module: the un-converted figure is what the board
                        # copied when it sized VZ at 19.2%.
                        invested = sum(held_values.values())
                        # A candidate not yet held would be funded from cash,
                        # so the target sleeve grows by its own weight.
                        target_invested = invested if ticker in held_values else (
                            invested / (1.0 - w_t) if w_t < 1.0 else invested
                        )
                        w_equity = w_t * target_invested / equity
                        already = held_values.get(ticker, 0.0) / equity
                        headroom = max(0.0, w_equity - already)
                        constraints["HRP ceiling (covariance-aware)"] = headroom
                        notes.append(
                            f"HRP target {_fmt_pct(w_t)} of INVESTED capital = "
                            f"{_fmt_pct(w_equity)} of equity (book is "
                            f"{_fmt_pct(cash / equity)} cash); already held "
                            f"{_fmt_pct(already)} → headroom {_fmt_pct(headroom)}"
                        )
        except Exception as e:
            logger.debug("[SizingBracket] %s: HRP leg failed: %s", ticker, e)

        # ── 3. Cash actually available ──
        if cash > 0:
            constraints["cash available"] = cash / equity
        else:
            constraints["cash available"] = 0.0
            notes.append("NO CASH — a BUY cannot be funded")

        # ── 4. Concentration cap ──
        try:
            conc_cap = float(get_param("MAX_CONCENTRATION_PCT"))
            already = held_values.get(ticker, 0.0) / equity
            constraints["concentration cap"] = max(0.0, conc_cap - already)
        except Exception as e:
            logger.debug("[SizingBracket] %s: concentration leg failed: %s", ticker, e)

        # ── 5. The hard cap, always present ──
        try:
            constraints["hard cap (MAX_POSITION_SIZE_PCT)"] = float(
                get_param("MAX_POSITION_SIZE_PCT")
            )
        except Exception:
            pass

        if not constraints:
            return ""

        binding_name = min(constraints, key=lambda k: constraints[k])
        binding_val = constraints[binding_name]

        lines = [
            "POSITION SIZING BRACKET (computed in code — all figures are a "
            "PERCENT OF PORTFOLIO EQUITY for THIS order):",
        ]
        for name, val in sorted(constraints.items(), key=lambda kv: kv[1]):
            mark = "  <== BINDS" if name == binding_name else ""
            lines.append(f"  - {name}: {_fmt_pct(val)}{mark}")
        for n in notes:
            lines.append(f"  · {n}")
        lines.append(
            f"  → Any BUY should be at or below {_fmt_pct(binding_val)} of equity. "
            f"Choose a size within this bracket and justify it; do NOT copy a "
            f"number from elsewhere in the briefing. The HRP figure is a "
            f"portfolio TARGET WEIGHT, not an order size."
        )
        if binding_val <= 0:
            lines.append(
                "  ⚠ The binding constraint is ZERO — there is no room for a "
                "BUY here. Say so rather than proposing one."
            )
        return "\n".join(lines)
    except Exception as e:
        logger.debug("[SizingBracket] %s: block failed (non-fatal): %s", ticker, e)
        return ""
