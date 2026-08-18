"""
Portfolio snapshots & equity curve — reads from PostgreSQL trading tables.

Uses actual schema:
  - portfolio_snapshots(id, bot_id, snapshot_ts, cash_balance, total_value, ...)
  - positions(id, bot_id, ticker, qty, avg_entry_price, stop_loss_pct)
  - orders(id, bot_id, ticker, side, qty, price, signal, created_at, filled_at)
  - bots(bot_id, cash_balance, total_pnl, total_trades, ...)

Usage:
    from app.trading.portfolio import (
        get_current_state, take_snapshot, get_equity_curve,
        get_recent_trades, get_performance_summary,
    )
"""

import logging
import uuid
import math
from datetime import datetime, timezone

from app.db.connection import get_db
from app.config import settings
from app.utils.tz import utc_iso
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


def _get_default_bot_id() -> str:
    """Resolve the active bot_id dynamically.

    Delegates to THE single resolver (2026-07-25 audit) rather than repeating
    the get_active_bot_id/settings.BOT_ID dance a fourth time. Kept as a named
    function because several call sites read `bot_id or _get_default_bot_id()`.

    The import MUST stay function-local: app.trading.paper_trader imports this
    module, and app.tools.portfolio_tools imports paper_trader at module level,
    so a top-level import here would close the cycle
    portfolio -> portfolio_tools -> paper_trader -> portfolio.
    """
    from app.tools.portfolio_tools import resolve_bot_id

    return resolve_bot_id()


def _safe_float(val, fallback=None):
    if val is None:
        return fallback
    try:
        fval = float(val)
        if math.isinf(fval) or math.isnan(fval):
            return fallback
        return fval
    except (ValueError, TypeError):
        return fallback


def get_current_state(bot_id: str = "") -> dict:
    """Return current portfolio: cash, positions, total value."""
    bid = bot_id or _get_default_bot_id()
    logger.info("[TRACE][STATE] resolved bid=%s (input was '%s')", bid, bot_id)

    # Bot state
    with get_db() as db:
        bot = mongo_query.find_row('bots', {'bot_id': bid}, ['cash_balance', 'total_pnl', 'total_trades'])

    cash = (
        _safe_float(bot[0], fallback=settings.STARTING_CASH)
        if bot
        else settings.STARTING_CASH
    )
    total_pnl = _safe_float(bot[1], fallback=0.0) if bot else 0.0

    # Latest snapshot timestamp
    with get_db() as db:
        snap = mongo_query.find_row('portfolio_snapshots', {'bot_id': bid}, ['snapshot_ts'], sort=[('snapshot_ts', -1)])

    updated_at = snap[0] if snap else None

    # Open positions and dynamic equity calculation
    positions = []
    equity = 0.0
    try:
        with get_db() as db:
            pos_rows = mongo_query.find_rows('positions', {'bot_id': bid, 'qty': {'$gt': 0}}, ['ticker', 'qty', 'avg_entry_price', 'stop_loss_pct'], sort=[('ticker', 1)])
            logger.info(
                "[TRACE][STATE] raw pos_rows count=%d for bot_id=%s", len(pos_rows), bid
            )
            for p in pos_rows:
                logger.info(
                    "[TRACE][STATE] raw row: ticker=%s qty=%s avg_entry=%s",
                    p[0],
                    p[1],
                    p[2],
                )
            for p in pos_rows:
                ticker = p[0]
                qty = _safe_float(p[1], fallback=0.0)
                avg_entry_price = _safe_float(p[2], fallback=0.0)
                stop_loss_pct = _safe_float(p[3], fallback=0.0)

                # Fetch live current price
                price_row = mongo_query.find_row('price_history', {'ticker': ticker}, ['close'], sort=[('date', -1)])
                if not price_row:
                    price_row = mongo_query.find_row('asset_prices', {'symbol': ticker}, ['close'], sort=[('date', -1)])

                curr_price = (
                    _safe_float(price_row[0], fallback=avg_entry_price)
                    if price_row and price_row[0]
                    else avg_entry_price
                )

                # ── Price sanity check ──
                # Prevent phantom gains from ticker collisions (e.g. ETH ETF vs ETH crypto)
                # or stale/corrupt price data.  If current price deviates >10x from entry,
                # the data is almost certainly wrong — fall back to entry price.
                if avg_entry_price and avg_entry_price > 0 and curr_price > 0:
                    ratio = curr_price / avg_entry_price
                    if ratio > 10 or ratio < 0.1:
                        logger.warning(
                            "price sanity: %s curr=$%.2f vs entry=$%.2f (%.1fx) — "
                            "using entry price to prevent phantom P&L",
                            ticker,
                            curr_price,
                            avg_entry_price,
                            ratio,
                        )
                        curr_price = avg_entry_price

                equity += qty * curr_price

                # Fetch extra data for table consistency
                meta = mongo_query.find_row('ticker_metadata', {'ticker': ticker}, ['sector', 'market_cap_tier', 'market_cap'])
                fund = mongo_query.find_row('fundamentals', {'ticker': ticker}, ['pe_ratio', 'revenue_growth'], sort=[('snapshot_date', -1)])
                tech = mongo_query.find_row('technicals', {'ticker': ticker}, ['rsi_14'], sort=[('date', -1)])

                positions.append(
                    {
                        "ticker": ticker,
                        "qty": qty,
                        "avg_entry_price": avg_entry_price,
                        "current_price": curr_price,
                        "stop_loss_pct": stop_loss_pct,
                        "sector": meta[0] if meta else None,
                        "market_cap_tier": meta[1] if meta else None,
                        "market_cap": _safe_float(meta[2]) if meta else None,
                        "pe_ratio": _safe_float(fund[0]) if fund else None,
                        "revenue_growth": _safe_float(fund[1]) if fund else None,
                        "rsi_14": _safe_float(tech[0]) if tech else None,
                    }
                )
    except Exception as e:
        logger.warning("positions query failed: %s", e)

    total_value = cash + equity

    logger.info(
        "[TRACE][STATE] positions returned=%d cash=%.2f total_value=%.2f for bot_id=%s",
        len(positions),
        cash,
        total_value,
        bid,
    )

    # Unrealized P&L: mark-to-market on open positions. Computed here because
    # `equity` already carries the sanity-checked current prices, so recomputing
    # it downstream would risk using a different price than total_value did and
    # produce a book that does not add up.
    cost_basis = sum(
        p["qty"] * p["avg_entry_price"] for p in positions if p.get("avg_entry_price")
    )
    unrealized_pnl = equity - cost_basis

    return {
        "bot_id": bid,
        "cash": cash,
        "total_value": total_value,
        "total_pnl": total_pnl,
        "realized_pnl": total_pnl,
        "unrealized_pnl": unrealized_pnl,
        "cost_basis": cost_basis,
        "equity": equity,
        "positions": positions,
        "position_count": len(positions),
        "updated_at": utc_iso(updated_at),
    }


def get_recent_trades(bot_id: str = "", limit: int = 50) -> list[dict]:
    """Return recent orders (paper trades)."""
    bid = bot_id or _get_default_bot_id()
    results = []
    with get_db() as db:
        rows = mongo_query.find_rows('orders', {'bot_id': bid}, ['ticker', 'side', 'qty', 'price', 'signal', 'created_at', 'filled_at', 'realized_pnl'], sort=[('created_at', -1)], limit=limit)
        for r in rows:
            ticker = r[0]
            meta = mongo_query.find_row('ticker_metadata', {'ticker': ticker}, ['sector', 'market_cap_tier', 'market_cap'])
            fund = mongo_query.find_row('fundamentals', {'ticker': ticker}, ['pe_ratio', 'revenue_growth'], sort=[('snapshot_date', -1)])
            tech = mongo_query.find_row('technicals', {'ticker': ticker}, ['rsi_14'], sort=[('date', -1)])

            results.append(
                {
                    "ticker": ticker,
                    "side": r[1],
                    "qty": _safe_float(r[2], fallback=0.0),
                    "price": _safe_float(r[3], fallback=0.0),
                    "signal": r[4],
                    "created_at": utc_iso(r[5]),
                    "filled_at": utc_iso(r[6]),
                    "realized_pnl": _safe_float(r[7], fallback=0.0),
                    "sector": meta[0] if meta else None,
                    "market_cap_tier": meta[1] if meta else None,
                    "market_cap": _safe_float(meta[2]) if meta else None,
                    "pe_ratio": _safe_float(fund[0]) if fund else None,
                    "revenue_growth": _safe_float(fund[1]) if fund else None,
                    "rsi_14": _safe_float(tech[0]) if tech else None,
                }
            )
    return results


def get_equity_curve(bot_id: str = "", days: int = 30) -> list[dict]:
    """Return equity curve data from portfolio_snapshots."""
    bid = bot_id or _get_default_bot_id()
    days = max(1, min(int(days), 365))
    with get_db() as db:
        rows = db.execute(
            "SELECT total_value, cash_balance, snapshot_ts, "
            "       realized_pnl, unrealized_pnl "
            "FROM portfolio_snapshots WHERE bot_id = %s "
            f"AND snapshot_ts >= CURRENT_TIMESTAMP - INTERVAL '{days} days' "
            "ORDER BY snapshot_ts ASC",
            [bid],
        ).fetchall()
    return [
        {
            "total_value": _safe_float(r[0], fallback=0.0),
            "cash": _safe_float(r[1], fallback=0.0),
            "timestamp": utc_iso(r[2]),
            # NULL on rows written before 2026-07-26 and deliberately surfaced as
            # None rather than 0.0: a zero would claim the book made nothing,
            # when the truth is that nobody recorded it.
            "realized_pnl": _safe_float(r[3]) if r[3] is not None else None,
            "unrealized_pnl": _safe_float(r[4]) if r[4] is not None else None,
        }
        for r in rows
    ]


def take_snapshot(bot_id: str = "") -> dict:
    """Take a point-in-time snapshot. Returns the state."""
    state = get_current_state(bot_id)
    bid = bot_id or _get_default_bot_id()
    now = datetime.now(timezone.utc)
    snap_id = str(uuid.uuid4())
    try:
        with get_db() as db:
            # realized/unrealized P&L were in the schema and never written — all
            # 25 rows carried NULL, so the equity curve (the only true bottom
            # line) could not be decomposed into "trades we closed" versus "marks
            # that moved". A column that exists and is never populated reads as a
            # measurement rather than a gap (2026-07-26 audit).
            mongo_store.insert_docs('portfolio_snapshots', [{'id': snap_id, 'bot_id': bid, 'snapshot_ts': now, 'cash_balance': state["cash"], 'total_value': state["total_value"], 'realized_pnl': round(state.get("realized_pnl") or 0.0, 4), 'unrealized_pnl': round(state.get("unrealized_pnl") or 0.0, 4)}])
        logger.info(
            "snapshot: %s total_value=%.2f realized=%.2f unrealized=%.2f",
            bid,
            state["total_value"],
            state.get("realized_pnl") or 0.0,
            state.get("unrealized_pnl") or 0.0,
        )
    except Exception as e:
        logger.error("snapshot failed: %s", e)
    return state


def get_performance_summary(bot_id: str = "") -> dict:
    """Calculate performance metrics from bots table."""
    bid = bot_id or _get_default_bot_id()
    state = get_current_state(bid)
    try:
        from app.services.bot_manager import get_bot_starting_cash

        starting = get_bot_starting_cash(bid)
    except Exception:
        starting = settings.STARTING_CASH
    total_val = state["total_value"]
    pnl = total_val - starting
    pnl_pct = (pnl / starting) * 100 if starting else 0

    # From bots table
    with get_db() as db:
        bot = mongo_query.find_row('bots', {'bot_id': bid}, ['total_trades', 'total_pnl', 'win_rate'])

    total_trades = bot[0] if bot else 0
    realized_pnl = bot[1] if bot else 0.0
    win_rate = bot[2] if bot else 0.0

    return {
        "bot_id": bid,
        "starting_cash": starting,
        "current_value": total_val,
        "cash": state["cash"],
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "realized_pnl": round(realized_pnl, 2),
        "win_rate": round(win_rate, 2),
        "total_trades": total_trades,
        "open_positions": state["position_count"],
    }


# ── BROKER LEDGER QUERIES ──


def get_position_lots(bot_id: str = "", ticker: str | None = None) -> list[dict]:
    """Get open/partial lots, optionally filtered by ticker."""
    bid = bot_id or _get_default_bot_id()
    with get_db() as db:
        if ticker:
            rows = mongo_query.find_rows('position_lots', {'bot_id': bid, 'ticker': ticker.upper(), 'status': {'$in': ['open', 'partial']}}, ['lot_id', 'ticker', 'opened_at', 'original_qty', 'remaining_qty', 'entry_price', 'status', 'cycle_id', 'is_legacy'], sort=[('opened_at', 1)])
        else:
            rows = mongo_query.find_rows('position_lots', {'bot_id': bid, 'status': {'$in': ['open', 'partial']}}, ['lot_id', 'ticker', 'opened_at', 'original_qty', 'remaining_qty', 'entry_price', 'status', 'cycle_id', 'is_legacy'], sort=[('ticker', 1), ('opened_at', 1)])
    return [
        {
            "lot_id": r[0],
            "ticker": r[1],
            "opened_at": utc_iso(r[2]),
            "original_qty": _safe_float(r[3], fallback=0.0),
            "remaining_qty": _safe_float(r[4], fallback=0.0),
            "entry_price": _safe_float(r[5], fallback=0.0),
            "status": r[6],
            "cycle_id": r[7],
            "is_legacy": bool(r[8]),
        }
        for r in rows
    ]


def get_lot_closures(
    bot_id: str = "", ticker: str | None = None, limit: int = 50
) -> list[dict]:
    """Get recent lot closures (realized trades with per-lot P&L)."""
    bid = bot_id or _get_default_bot_id()
    limit = max(1, min(int(limit), 500))
    params: list = [bid]
    where_extra = ""
    if ticker:
        where_extra = " AND ticker = %s"
        params.append(ticker.upper())
    params.append(limit)
    with get_db() as db:
        rows = db.execute(
            "SELECT closure_id, ticker, closed_qty, entry_price, exit_price, "
            "realized_pnl, closed_at, holding_days, lot_id "
            "FROM lot_closures "
            f"WHERE bot_id = %s{where_extra} "
            "ORDER BY closed_at DESC LIMIT %s",
            params,
        ).fetchall()
    return [
        {
            "closure_id": r[0],
            "ticker": r[1],
            "closed_qty": _safe_float(r[2], fallback=0.0),
            "entry_price": _safe_float(r[3], fallback=0.0),
            "exit_price": _safe_float(r[4], fallback=0.0),
            "realized_pnl": _safe_float(r[5], fallback=0.0),
            "closed_at": utc_iso(r[6]),
            "holding_days": r[7],
            "lot_id": r[8],
        }
        for r in rows
    ]


def get_trade_fills(bot_id: str = "", limit: int = 50) -> list[dict]:
    """Get recent trade fills from the broker ledger."""
    bid = bot_id or _get_default_bot_id()
    limit = max(1, min(int(limit), 500))
    with get_db() as db:
        rows = mongo_query.find_rows('trade_fills', {'bot_id': bid}, ['fill_id', 'order_id', 'ticker', 'side', 'fill_qty', 'fill_price', 'fill_value', 'fees', 'filled_at', 'cycle_id'], sort=[('filled_at', -1)], limit=limit)
    return [
        {
            "fill_id": r[0],
            "order_id": r[1],
            "ticker": r[2],
            "side": r[3],
            "fill_qty": _safe_float(r[4], fallback=0.0),
            "fill_price": _safe_float(r[5], fallback=0.0),
            "fill_value": _safe_float(r[6], fallback=0.0),
            "fees": _safe_float(r[7], fallback=0.0),
            "filled_at": utc_iso(r[8]),
            "cycle_id": r[9],
        }
        for r in rows
    ]


def get_lot_count_by_ticker(bot_id: str = "") -> dict[str, int]:
    """Return {ticker: open_lot_count} for all open positions."""
    bid = bot_id or _get_default_bot_id()
    with get_db() as db:
        rows = mongo_query.group_rows('position_lots', {'bot_id': bid, 'status': {'$in': ['open', 'partial']}}, ['ticker'], [('count', None)], [('key', 'ticker'), ('agg', 0)])
    return {r[0]: r[1] for r in rows}
