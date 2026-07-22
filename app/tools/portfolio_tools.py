import json
import logging
from datetime import datetime, timezone
from app.db.connection import get_db
from app.config import settings
from app.tools.registry import registry
from app.trading.paper_trader import _get_current_price

logger = logging.getLogger(__name__)

def get_position_context(ticker: str, bot_id: str = "") -> dict:
    if not bot_id:
        bot_id = settings.BOT_ID

    with get_db() as db:
        ticker = ticker.upper()

        try:
            row = db.execute(
                "SELECT qty, avg_entry_price, stop_loss_pct, opened_at "
                "FROM positions WHERE bot_id = %s AND ticker = %s",
                [bot_id, ticker],
            ).fetchone()
        except Exception as e:
            logger.debug(
                "[POSITION_CTX] Failed to query position for %s: %s",
                ticker,
                e,
            )
            return {"held": False}

        if not row:
            return {"held": False}

        qty = float(row[0])
        avg_entry = float(row[1])
        stop_loss_pct = float(row[2]) if row[2] else 0.0
        opened_at = row[3]

        current_price, _ = _get_current_price(ticker)
        unrealized_pnl = 0.0
        unrealized_pnl_pct = 0.0

        if current_price and current_price > 0:
            unrealized_pnl = (current_price - avg_entry) * qty
            unrealized_pnl_pct = ((current_price - avg_entry) / avg_entry) * 100

        stop_price = avg_entry * (1 - (stop_loss_pct / 100.0))
        holding_days = 0
        if opened_at:
            # opened_at comes back tz-naive from the DB; subtracting it from a
            # tz-aware now() raised "can't subtract offset-naive and
            # offset-aware datetimes" — caught upstream, but it silently
            # dropped portfolio_context from EVERY cycle's agent prompts.
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            holding_days = (datetime.now(timezone.utc) - opened_at).days

        ctx = {
            "held": True,
            "qty": qty,
            "avg_entry": avg_entry,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "holding_days": holding_days,
            "stop_loss_pct": stop_loss_pct,
            "stop_price": stop_price,
        }

        # Try to pull original thesis
        try:
            thesis_row = db.execute(
                "SELECT thesis, timestamp, confidence_score "
                "FROM trade_history "
                "WHERE bot_id = %s AND ticker = %s AND action = 'BUY' "
                "ORDER BY timestamp DESC LIMIT 1",
                [bot_id, ticker],
            ).fetchone()

            if thesis_row:
                ctx["original_thesis"] = thesis_row[0]
                ctx["original_thesis_date"] = (
                    thesis_row[1].isoformat() if thesis_row[1] else None
                )
                ctx["original_thesis_conf"] = thesis_row[2]
        except Exception:
            pass

        return ctx


# ── Tool Registration ──────────────────────────────────────────────
# These two were whitelisted for user_chat, the quant analyst, and the board
# ("Phase 2: contextual portfolio awareness") but never had an implementation —
# every call raised "no local registration function".

@registry.register(
    name="get_portfolio_state",
    description=(
        "Get the bot's full portfolio state: cash balance, total P&L, every open "
        "position (qty, entry price, stop-loss, opened date, mark-to-market value), "
        "position count, and total portfolio value. Use this to understand current "
        "exposure before sizing or recommending a trade."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    tier=0,
    source="paper_trader",
)
async def get_portfolio_state_tool(**_extra) -> str:
    from app.trading.paper_trader import get_portfolio

    bot_id = settings.BOT_ID
    portfolio = get_portfolio(bot_id)

    total_position_value = 0.0
    for p in portfolio.get("positions", []):
        price, _ = _get_current_price(p["ticker"])
        if price is None:
            price = p["avg_entry_price"]
        p["current_price"] = price
        p["market_value"] = round(float(p["qty"]) * float(price), 2)
        entry = float(p["avg_entry_price"]) or 0.0
        p["unrealized_pnl_pct"] = (
            round(((float(price) - entry) / entry) * 100, 2) if entry else None
        )
        total_position_value += p["market_value"]

    portfolio["total_position_value"] = round(total_position_value, 2)
    portfolio["total_value"] = round(float(portfolio.get("cash") or 0.0) + total_position_value, 2)
    return json.dumps(portfolio, default=str)


@registry.register(
    name="get_position_pnl",
    description=(
        "Check the P&L and health of a specific held position: entry price, current "
        "price, unrealized P&L (absolute and %), holding duration, stop-loss price, "
        "and the original buy thesis. Returns held=false if the bot has no position "
        "in the ticker."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "The stock ticker to check."},
        },
        "required": ["ticker"],
    },
    tier=0,
    source="paper_trader",
)
async def get_position_pnl_tool(ticker: str, **_extra) -> str:
    return json.dumps(get_position_context(ticker), default=str)


# ── Portfolio-level math tools ─────────────────────────────────────
# Covariance shrinkage / HRP / diversification ratio. Until these existed,
# every sizing decision was single-ticker: calculate_position_size knows the
# cash balance but nothing about how correlated the new position is to what
# is already held.

_MAX_UNIVERSE = 20  # keep matrices (and tool output) agent-context sized


def _current_holdings(bot_id: str = "") -> tuple[dict[str, float], float, float]:
    """(market_value per held ticker, cash, total equity) for the active bot."""
    from app.trading.paper_trader import get_portfolio

    portfolio = get_portfolio(bot_id or settings.BOT_ID)
    cash = float(portfolio.get("cash") or 0.0)
    values: dict[str, float] = {}
    for p in portfolio.get("positions", []):
        price, _ = _get_current_price(p["ticker"])
        if price is None:
            price = p["avg_entry_price"]
        values[p["ticker"].upper()] = float(p["qty"]) * float(price)
    return values, cash, cash + sum(values.values())


def _resolve_universe(tickers: str, extra: list[str] | None = None) -> tuple[list[str], dict[str, float], float]:
    """Ticker universe for portfolio math: explicit CSV list, else current
    holdings; `extra` (e.g. a BUY candidate) is unioned in. Also returns the
    held market values and total equity."""
    values, _cash, equity = _current_holdings()
    universe = (
        [t.strip().upper() for t in tickers.split(",") if t.strip()]
        if tickers.strip()
        else sorted(values)
    )
    for t in extra or []:
        t = t.strip().upper()
        if t and t not in universe:
            universe.append(t)
    return universe[:_MAX_UNIVERSE], values, equity


@registry.register(
    name="get_portfolio_covariance",
    description=(
        "Estimate the Ledoit-Wolf shrunk covariance/correlation structure of a "
        "set of tickers (default: the bot's current holdings) from daily returns "
        "in price_history. Returns shrinkage intensity, matrix condition number "
        "(>1000 = near-singular, optimizer output would be garbage), per-ticker "
        "annualized vol, average pairwise correlation, and the most/least "
        "correlated pairs. Use before sizing a BUY to see whether the candidate "
        "actually diversifies the book."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tickers": {
                "type": "string",
                "description": "Comma-separated tickers. Empty = current holdings.",
            },
            "lookback_days": {
                "type": "integer",
                "description": "Trading days of history (default 252).",
            },
        },
        "required": [],
    },
    tier=0,
    source="portfolio_math",
)
async def get_portfolio_covariance_tool(
    tickers: str = "", lookback_days: int = 252, **_extra
) -> str:
    from app.quant import portfolio_math
    from app.quant.returns import load_returns_matrix

    universe, _values, _equity = _resolve_universe(tickers)
    if len(universe) < 2:
        return json.dumps({
            "error": "Need at least 2 tickers (portfolio holds fewer than 2 — "
                     "pass tickers explicitly).",
            "universe": universe,
        })

    returns, dropped = load_returns_matrix(universe, int(lookback_days))
    if returns.shape[1] < 2:
        return json.dumps({"error": "Insufficient overlapping price history.", "dropped": dropped})

    kept = list(returns.columns)
    matrix = returns.fillna(0.0).values
    cov, intensity = portfolio_math.ledoit_wolf_shrinkage(matrix)
    cond = portfolio_math.condition_number(cov)
    corr = portfolio_math.cov_to_corr(cov)

    n = len(kept)
    ann_vol = {
        kept[i]: round(float((cov[i, i] ** 0.5) * (252 ** 0.5) * 100), 2)
        for i in range(n)
    }
    pairs = [
        (kept[i], kept[j], round(float(corr[i, j]), 3))
        for i in range(n) for j in range(i + 1, n)
    ]
    pairs.sort(key=lambda p: p[2])
    out = {
        "tickers": kept,
        "dropped_low_coverage": dropped,
        "observations": int(returns.shape[0]),
        "shrinkage_intensity": round(intensity, 4),
        "condition_number": round(cond, 1),
        "condition_warning": "HIGH_CONDITION" if cond > 1000 else "OK",
        "annualized_vol_pct": ann_vol,
        "avg_pairwise_correlation": round(float(sum(p[2] for p in pairs) / len(pairs)), 3) if pairs else None,
        "least_correlated_pairs": [f"{a}-{b}: {c}" for a, b, c in pairs[:3]],
        "most_correlated_pairs": [f"{a}-{b}: {c}" for a, b, c in pairs[-3:]],
    }
    if n <= 12:
        out["correlation_matrix"] = {
            kept[i]: {kept[j]: round(float(corr[i, j]), 2) for j in range(n)}
            for i in range(n)
        }
    return json.dumps(out)


@registry.register(
    name="calculate_hrp_allocation",
    description=(
        "Compute Hierarchical Risk Parity target weights over the current "
        "holdings (optionally plus a BUY candidate) using a Ledoit-Wolf shrunk "
        "covariance — covariance-aware position sizing that never inverts the "
        "matrix. Optional views (simplified Black-Litterman tilt) scale weights "
        "by direction and confidence. Returns target weights, dollar "
        "allocations at current equity, the portfolio diversification ratio, "
        "current-vs-target drift, and any >5% drift breaches. Use this as the "
        "sizing baseline instead of flat cash-percent sizing when portfolio "
        "context matters."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tickers": {
                "type": "string",
                "description": "Comma-separated universe. Empty = current holdings.",
            },
            "candidate_ticker": {
                "type": "string",
                "description": "Ticker being considered for a new BUY — unioned into the universe.",
            },
            "views": {
                "type": "string",
                "description": (
                    'JSON list of directional views, e.g. '
                    '[{"ticker":"NVDA","direction":"BULLISH","confidence":70}]. '
                    "Low confidence barely moves the allocation."
                ),
            },
            "lookback_days": {
                "type": "integer",
                "description": "Trading days of history (default 252).",
            },
        },
        "required": [],
    },
    tier=0,
    source="portfolio_math",
)
async def calculate_hrp_allocation_tool(
    tickers: str = "",
    candidate_ticker: str = "",
    views: str = "",
    lookback_days: int = 252,
    **_extra,
) -> str:
    from app.quant import portfolio_math
    from app.quant.returns import load_returns_matrix

    universe, held_values, equity = _resolve_universe(
        tickers, [candidate_ticker] if candidate_ticker.strip() else None
    )
    if len(universe) < 2:
        return json.dumps({
            "error": "Need at least 2 tickers for a portfolio allocation "
                     "(holdings plus candidate came to fewer than 2).",
            "universe": universe,
        })

    returns, dropped = load_returns_matrix(universe, int(lookback_days))
    if returns.shape[1] < 2:
        return json.dumps({"error": "Insufficient overlapping price history.", "dropped": dropped})

    kept = list(returns.columns)
    cov, intensity = portfolio_math.ledoit_wolf_shrinkage(returns.fillna(0.0).values)
    weights_arr = portfolio_math.hrp_weights(cov)
    weights = {kept[i]: float(weights_arr[i]) for i in range(len(kept))}

    parsed_views: list[dict] = []
    if views.strip():
        try:
            parsed_views = json.loads(views)
        except (json.JSONDecodeError, TypeError):
            parsed_views = []
    if parsed_views:
        weights = portfolio_math.apply_view_tilt(weights, parsed_views)

    final_arr = [weights[t] for t in kept]
    dr = portfolio_math.diversification_ratio(final_arr, cov)

    held_total = sum(held_values.values())
    current_weights = (
        {t: held_values.get(t, 0.0) / held_total for t in kept} if held_total > 0 else {t: 0.0 for t in kept}
    )
    drift = portfolio_math.rebalance_drift(current_weights, weights, threshold=0.05)

    candidate = candidate_ticker.strip().upper()
    out = {
        "target_weights": {t: round(w, 4) for t, w in weights.items()},
        "dollar_allocations": {t: round(w * equity, 2) for t, w in weights.items()},
        "total_equity": round(equity, 2),
        "diversification_ratio": round(dr, 3),
        "shrinkage_intensity": round(intensity, 4),
        "condition_number": round(portfolio_math.condition_number(cov), 1),
        "views_applied": len(parsed_views),
        "dropped_low_coverage": dropped,
        "current_weights": {t: round(w, 4) for t, w in current_weights.items()},
        "drift_breaches_over_5pct": {t: round(d, 4) for t, d in drift["breaches"].items()},
    }
    if candidate and candidate in weights:
        out["candidate_note"] = (
            f"HRP allocates {candidate} {weights[candidate] * 100:.1f}% of equity "
            f"(${weights[candidate] * equity:,.0f}) given its covariance with the "
            "existing book — treat this as the sizing baseline."
        )
    return json.dumps(out)


@registry.register(
    name="get_strategy_health",
    description=(
        "Model-degradation check, independent of P&L: quality-score history "
        "(avg + trend) for the decision-critical pipeline agents from "
        "v3_agent_telemetry. Statuses: NORMAL, REDUCE (BUY sizes halved), CUT "
        "(new BUYs policy-blocked). Optionally pass one agent_name for detail."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Specific agent to check (default: all decision-critical agents).",
            },
        },
        "required": [],
    },
    tier=0,
    source="portfolio_math",
)
async def get_strategy_health_tool(agent_name: str = "", **_extra) -> str:
    from app.quant.strategy_health import agent_health, get_pipeline_health

    if agent_name.strip():
        return json.dumps(agent_health(agent_name.strip()))
    return json.dumps(get_pipeline_health())
