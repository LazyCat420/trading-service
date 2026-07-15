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
