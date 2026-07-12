import logging
from datetime import datetime, timezone
from app.db.connection import get_db
from app.config import settings
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
