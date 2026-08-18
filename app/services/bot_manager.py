"""Bot Profile Manager — centralized service for multi-profile trading.

All backend code should use these functions to resolve the active bot,
instead of reading settings.BOT_ID directly. This enables profile switching
without restarting the server.

Public API:
    get_active_bot_id()         → str
    get_bot_starting_cash(bid)  → float
    set_active_bot(bot_id)      → None
    is_cycle_running()          → bool
    create_bot_profile(...)     → dict
    delete_bot_profile(bot_id)  → dict
    reset_bot_profile(bot_id)   → dict
    list_bot_profiles()         → list[dict]
"""

import logging
import re
import time
import uuid
from datetime import datetime, timezone

from app.config import settings
from app.db.connection import get_db
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

# ── In-memory cache of the active bot_id ──
# Initialized lazily from DB on first call, updated on set_active_bot().
# TTL-based: expires after _CACHE_TTL_S seconds to prevent stale fallback caching.
_active_bot_id: str | None = None
_active_bot_id_ts: float = 0.0  # monotonic timestamp of last cache write
_CACHE_TTL_S: float = 120.0  # 2 minute TTL


def _slugify(name: str) -> str:
    """Convert display name to a URL-safe bot_id slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)  # remove special chars
    slug = re.sub(r"[\s]+", "-", slug)  # spaces → hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")  # collapse hyphens
    return slug or f"bot-{uuid.uuid4().hex[:8]}"


def get_active_bot_id() -> str:
    """Return the currently active bot_id.

    Resolution order:
    1. In-memory cache (fastest, with TTL)
    2. DB lookup (is_active = TRUE)
    3. Fallback to settings.BOT_ID
    """
    global _active_bot_id, _active_bot_id_ts
    now = time.monotonic()

    # Return cached value if within TTL
    if _active_bot_id is not None and (now - _active_bot_id_ts) < _CACHE_TTL_S:
        logger.debug(
            "[TRACE][BOT_MANAGER] get_active_bot_id() cache hit: %s", _active_bot_id
        )
        return _active_bot_id

    try:
        with get_db() as db:
            rows = mongo_query.find_rows('bots', {'is_active': True}, ['bot_id'], sort=[('created_at', 1)])
            if rows:
                _active_bot_id = rows[0][0]
                _active_bot_id_ts = now
                # Enforce single-active invariant: if multiple are active, fix it
                if len(rows) > 1:
                    logger.warning(
                        "[BOT_MANAGER] INVARIANT VIOLATION: %d bots marked active! "
                        "Keeping '%s', deactivating others: %s",
                        len(rows),
                        _active_bot_id,
                        [r[0] for r in rows[1:]],
                    )
                    mongo_store.update_docs('bots', {'is_active': True, 'bot_id': {'$ne': _active_bot_id}}, {'$set': {'is_active': False}})
                logger.info(
                    "[TRACE][BOT_MANAGER] get_active_bot_id() from DB: %s",
                    _active_bot_id,
                )
                return _active_bot_id
    except Exception as e:
        logger.warning("Failed to query active bot: %s", e)

    _active_bot_id = settings.BOT_ID
    _active_bot_id_ts = now  # TTL applies to fallback too — prevents permanent stale cache
    logger.info(
        "[TRACE][BOT_MANAGER] get_active_bot_id() fallback to settings.BOT_ID: %s",
        _active_bot_id,
    )
    return _active_bot_id


def resolve_bot_id(bot_id: str | None) -> str:
    """Resolve bot_id: if default, empty, or None, use the active bot.
    Handles fallbacks internally.
    """
    if not bot_id or bot_id == "default":
        try:
            return get_active_bot_id()
        except Exception:
            return settings.BOT_ID
    return bot_id


def get_bot_starting_cash(bot_id: str = "") -> float:
    """Return the starting cash for a bot profile.

    Falls back to settings.STARTING_CASH if the bot doesn't have
    a starting_cash column or the bot doesn't exist.
    """
    bid = bot_id or get_active_bot_id()
    try:
        with get_db() as db:
            row = mongo_query.find_row('bots', {'bot_id': bid}, ['starting_cash'])
            if row and row[0] is not None:
                return float(row[0])
    except Exception as e:
        logger.debug("starting_cash lookup failed for %s: %s", bid, e)
    return settings.STARTING_CASH


def get_bot_description(bot_id: str = "") -> str:
    """Return the custom description/instructions for a bot profile.

    This is used to inject custom trading personas into the RLM system prompt.
    """
    bid = bot_id or get_active_bot_id()
    try:
        with get_db() as db:
            row = mongo_query.find_row('bots', {'bot_id': bid}, ['description'])
            if row and row[0]:
                return str(row[0]).strip()
    except Exception as e:
        logger.debug("description lookup failed for %s: %s", bid, e)
    return ""


def set_active_bot(bot_id: str) -> None:
    """Switch the active bot profile.

    Sets is_active=FALSE on all bots, then TRUE on the target.
    Raises ValueError if bot_id doesn't exist or a cycle is running.
    """
    global _active_bot_id, _active_bot_id_ts

    if is_cycle_running():
        raise ValueError(
            "Cannot switch profiles while a pipeline cycle is running. "
            "Stop the cycle first."
        )

    with get_db() as db:
        # Verify bot exists
        exists = db.execute("SELECT 1 FROM bots WHERE bot_id = %s", [bot_id]).fetchone()
        if not exists:
            raise ValueError(f"Bot profile '{bot_id}' does not exist")

        # Deactivate all, activate target
        mongo_store.update_docs('bots', {'is_active': True}, {'$set': {'is_active': False}})
        mongo_store.update_docs('bots', {'bot_id': bot_id}, {'$set': {'is_active': True}})
    _active_bot_id = bot_id
    _active_bot_id_ts = time.monotonic()
    logger.info("[BOT_MANAGER] Active bot switched to: %s", bot_id)

    # Reset pipeline state — the old bot's cycle state is irrelevant to the new bot.
    # Any interrupted/stopped checkpoint belongs to the previous bot and should not
    # be offered for resume under a different profile.
    from app.services.pipeline_state import PipelineStateDB

    PipelineStateDB.save_state(PipelineStateDB.default_state())
    logger.info("[BOT_MANAGER] Pipeline state reset to idle for new profile %s", bot_id)


def is_cycle_running() -> bool:
    """Check if a pipeline cycle is currently running."""
    try:
        from app.services.pipeline_service import PipelineService

        status = PipelineService.get_current_state(summary_only=True)
        # V3 pipeline reports status="running" with the stage in "phase";
        # the stage names below are the legacy (v1) status vocabulary.
        return status.get("status") in (
            "running",
            "collecting",
            "analyzing",
            "trading",
            "starting",
        )
    except Exception:
        return False


def list_bot_profiles() -> list[dict]:
    """Return all bot profiles with summary stats."""
    with get_db() as db:
        rows = mongo_query.find_rows('bots', {}, ['bot_id', 'display_name', 'model_name', 'status', 'cash_balance', 'starting_cash', 'total_pnl', 'win_rate', 'total_trades', 'is_active', 'created_at', 'last_run_at', 'description'], sort=[('is_active', -1), ('created_at', 1)])
    return [
        {
            "bot_id": r[0],
            "display_name": r[1] or r[0],
            "model_name": r[2],
            "status": r[3] or "idle",
            "cash_balance": float(r[4]) if r[4] else 0.0,
            "starting_cash": float(r[5]) if r[5] else settings.STARTING_CASH,
            "total_pnl": float(r[6]) if r[6] else 0.0,
            "win_rate": float(r[7]) if r[7] else 0.0,
            "total_trades": r[8] or 0,
            "is_active": bool(r[9]),
            "created_at": r[10].isoformat() if r[10] else None,
            "last_run_at": r[11].isoformat() if r[11] else None,
            "description": r[12] or "",
        }
        for r in rows
    ]

def create_bot_profile(
    display_name: str,
    starting_cash: float = 100_000.0,
    description: str = "",
) -> dict:
    """Create a new bot profile.

    Returns the created profile dict.
    Raises ValueError if slug collides with existing bot_id.
    """
    bot_id = _slugify(display_name)
    start_time = time.perf_counter()

    # Handle slug collisions
    db_start = time.perf_counter()
    with get_db() as db:
        db_acquired = time.perf_counter()
        existing = db.execute(
            "SELECT 1 FROM bots WHERE bot_id = %s", [bot_id]
        ).fetchone()

        if existing:
            # Append short UUID suffix
            bot_id = f"{bot_id}-{uuid.uuid4().hex[:6]}"

        now = datetime.now(timezone.utc)
        insert_start = time.perf_counter()
        mongo_store.insert_docs('bots', [{'bot_id': bot_id, 'display_name': display_name, 'model_name': settings.ACTIVE_MODEL, 'status': 'idle', 'cash_balance': starting_cash, 'starting_cash': starting_cash, 'total_pnl': 0.0, 'win_rate': 0.0, 'total_trades': 0, 'is_active': False, 'created_at': now, 'description': description}])
        insert_end = time.perf_counter()

    total_end = time.perf_counter()

    logger.info(
        "[BOT_MANAGER] Created profile: %s (%s) with $%.2f "
        "[Timing: get_db=%.3fs, insert=%.3fs, total=%.3fs]",
        display_name,
        bot_id,
        starting_cash,
        db_acquired - db_start,
        insert_end - insert_start,
        total_end - start_time,
    )
    return {
        "bot_id": bot_id,
        "display_name": display_name,
        "starting_cash": starting_cash,
        "description": description,
        "created_at": now.isoformat(),
    }


def update_bot_profile(
    bot_id: str,
    display_name: str | None = None,
    description: str | None = None,
    starting_cash: float | None = None,
) -> dict:
    """Update a bot profile's metadata.

    starting_cash can only be updated if the bot has 0 trades.
    """
    with get_db() as db:
        row = mongo_query.find_row('bots', {'bot_id': bot_id}, ['display_name', 'description', 'starting_cash', 'total_trades'])
        if not row:
            raise ValueError(f"Bot profile '{bot_id}' does not exist")

        current_name, current_desc, current_cash, trades = row

        if starting_cash is not None and trades and trades > 0:
            raise ValueError(
                f"Cannot change starting cash for '{bot_id}' — "
                f"it already has {trades} trades. Reset the profile first."
            )

        new_name = display_name if display_name is not None else current_name
        new_desc = description if description is not None else current_desc
        new_cash = starting_cash if starting_cash is not None else current_cash

        mongo_store.update_docs('bots', {'bot_id': bot_id}, {'$set': {'display_name': new_name, 'description': new_desc, 'starting_cash': new_cash}})

        # If starting_cash changed and no trades, also update cash_balance
        if starting_cash is not None:
            mongo_store.update_docs('bots', {'bot_id': bot_id}, {'$set': {'cash_balance': new_cash}})

    logger.info("[BOT_MANAGER] Updated profile: %s", bot_id)
    return {"bot_id": bot_id, "display_name": new_name, "updated": True}


def reset_bot_profile(bot_id: str) -> dict:
    """Reset a bot profile to its starting cash.

    Wipes: positions, orders, trade_fills, position_lots,
           lot_closures, portfolio_snapshots, decision_outcomes,
           analysis_results for this bot_id.
    Resets: cash_balance, total_pnl, total_trades, win_rate.
    """
    if is_cycle_running():
        raise ValueError("Cannot reset while a pipeline cycle is running")

    with get_db() as db:
        row = mongo_query.find_row('bots', {'bot_id': bot_id}, ['starting_cash'])
        if not row:
            raise ValueError(f"Bot profile '{bot_id}' does not exist")

        starting_cash = float(row[0]) if row[0] else settings.STARTING_CASH

        # Wipe trading data for this bot
        tables_to_clear = [
            "positions",
            "orders",
            "trade_fills",
            "position_lots",
            "lot_closures",
            "portfolio_snapshots",
        ]
        cleared = {}
        for table in tables_to_clear:
            try:
                result = db.execute(f"DELETE FROM {table} WHERE bot_id = %s", [bot_id])
                # psycopg doesn't return rowcount easily via our wrapper,
                # but the delete still works
                cleared[table] = "cleared"
            except Exception as e:
                cleared[table] = f"error: {e}"

        # Reset bot stats
        mongo_store.update_docs('bots', {'bot_id': bot_id}, {'$set': {'cash_balance': starting_cash, 'total_pnl': 0.0, 'total_trades': 0, 'win_rate': 0.0, 'status': 'idle'}})

    logger.info(
        "[BOT_MANAGER] Reset profile '%s' to $%.2f",
        bot_id,
        starting_cash,
    )
    return {
        "bot_id": bot_id,
        "starting_cash": starting_cash,
        "cleared_tables": cleared,
        "reset": True,
    }


_IMPORT_TABLES = (
    "positions",
    "orders",
    "trade_fills",
    "position_lots",
    "lot_closures",
    "portfolio_snapshots",
)


def import_positions(
    bot_id: str,
    positions: list[dict],
    cash: float,
    mode: str = "replace",
    set_starting_cash: bool = True,
) -> dict:
    """Seed a profile with real holdings brought in from a brokerage export.

    Each imported holding becomes a full ledger entry, not just a `positions`
    row: a synthetic BUY fill (source='import') plus one open lot marked
    `is_legacy`. Without the lot, the first SELL has nothing to close against
    and lot-level realized P&L is wrong for the life of the profile.

    Entry price is the REAL cost basis the user paid. That keeps P&L honest at
    the cost of making stops relative to a price from years ago, which cuts
    both ways: a long-held winner's stop sits far below the market, and a
    long-held loser is already through its stop the moment it lands. Imported
    positions therefore get exit_style='reanalyze_on_breach' so the background
    monitor hands a breach to the agent instead of liquidating an underwater
    holding the user just brought in.

    starting_cash is set to cash + total cost basis: the capital actually put
    in, so equity-vs-starting_cash reads as true lifetime return. total_pnl
    and total_trades are deliberately left alone — an import is not a trade
    the bot made, and the scorecard must not count it as one.
    """
    if mode not in ("replace", "merge"):
        raise ValueError("mode must be 'replace' or 'merge'")
    if is_cycle_running():
        raise ValueError(
            "Cannot import positions while a pipeline cycle is running. "
            "Stop the cycle first."
        )

    now = datetime.now(timezone.utc)
    total_cost = sum(
        float(p["quantity"]) * float(p["cost_per_share"]) for p in positions
    )

    with get_db() as db:
        row = mongo_query.find_row('bots', {'bot_id': bot_id}, ['starting_cash'])
        if not row:
            raise ValueError(f"Bot profile '{bot_id}' does not exist")

        with db.transaction():
            if mode == "replace":
                for table in _IMPORT_TABLES:
                    try:
                        db.execute(f"DELETE FROM {table} WHERE bot_id = %s", [bot_id])
                    except Exception as e:
                        logger.warning("import: clearing %s for %s: %s", table, bot_id, e)

            imported, merged = [], []
            for p in positions:
                ticker = str(p["ticker"]).strip().upper()
                qty = float(p["quantity"])
                price = float(p["cost_per_share"])
                if qty <= 0 or price <= 0:
                    raise ValueError(f"{ticker}: quantity and cost basis must be positive")

                opened_at = _parse_opened_at(p.get("opened_at")) or now
                stop_pct = p.get("stop_loss_pct")
                stop_pct = float(stop_pct) if stop_pct else _default_stop_pct(ticker)

                existing = mongo_query.find_row('positions', {'bot_id': bot_id, 'ticker': ticker}, ['id', 'qty', 'avg_entry_price'])

                if existing:
                    old_id, old_qty, old_price = existing
                    new_qty = float(old_qty) + qty
                    new_avg = (
                        float(old_qty) * float(old_price) + qty * price
                    ) / new_qty
                    mongo_store.update_docs('positions', {'id': old_id}, {'$set': {'qty': new_qty, 'avg_entry_price': round(new_avg, 6)}})
                    merged.append(ticker)
                else:
                    mongo_store.insert_docs('positions', [{'id': str(uuid.uuid4()), 'bot_id': bot_id, 'ticker': ticker, 'qty': qty, 'avg_entry_price': price, 'stop_loss_pct': stop_pct, 'stop_source': 'imported', 'exit_style': 'reanalyze_on_breach', 'opened_at': opened_at}])
                    imported.append(ticker)

                # Ledger: synthetic BUY fill + the open lot it created.
                order_id = str(uuid.uuid4())
                fill_id = str(uuid.uuid4())
                mongo_store.insert_docs('orders', [{'id': order_id, 'bot_id': bot_id, 'ticker': ticker, 'side': 'BUY', 'qty': qty, 'price': price, 'signal': 'import', 'created_at': opened_at, 'filled_at': opened_at}])
                mongo_store.insert_docs('trade_fills', [{'fill_id': fill_id, 'order_id': order_id, 'bot_id': bot_id, 'ticker': ticker, 'side': 'BUY', 'fill_qty': qty, 'fill_price': price, 'fill_value': round(qty * price, 2), 'fees': 0.0, 'filled_at': opened_at, 'source': 'import'}])
                mongo_store.insert_docs('position_lots', [{'lot_id': str(uuid.uuid4()), 'bot_id': bot_id, 'ticker': ticker, 'fill_id': fill_id, 'opened_at': opened_at, 'original_qty': qty, 'remaining_qty': qty, 'entry_price': price, 'status': 'open', 'is_legacy': True}])

            if set_starting_cash:
                mongo_store.update_docs('bots', {'bot_id': bot_id}, {'$set': {'cash_balance': cash, 'starting_cash': round(cash + total_cost, 2), 'status': 'idle'}})
            else:
                mongo_store.update_docs('bots', {'bot_id': bot_id}, {'$set': {'cash_balance': cash, 'status': 'idle'}})

    logger.info(
        "[BOT_MANAGER] Imported %d positions (%d merged) into '%s' — "
        "cost basis $%.2f, cash $%.2f, mode=%s",
        len(imported) + len(merged), len(merged), bot_id, total_cost, cash, mode,
    )
    return {
        "bot_id": bot_id,
        "imported": True,
        "mode": mode,
        "positions_created": len(imported),
        "positions_merged": len(merged),
        "tickers": imported + merged,
        "cost_basis": round(total_cost, 2),
        "cash": cash,
        "starting_cash": round(cash + total_cost, 2) if set_starting_cash else None,
    }


def _default_stop_pct(ticker: str) -> float:
    """Asset-class default stop for an imported holding.

    Deliberately NOT the ATR-derived stop paper_trader computes at buy time:
    that formula is `ATR * k / entry_price`, and an entry price from years ago
    makes it meaningless — a 10x winner would get a sub-1% stop.
    """
    from app.trading.paper_trader import _STOP_BOUNDS

    try:
        from app.config.config_tickers import classify_asset

        return _STOP_BOUNDS[classify_asset(ticker)][2]
    except Exception:
        return _STOP_BOUNDS["stock"][2]


def _parse_opened_at(raw) -> datetime | None:
    """Accept an ISO date/datetime string from the import payload."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def delete_bot_profile(bot_id: str) -> dict:
    """Delete a bot profile and ALL its trading data.

    Cannot delete the currently active profile.
    """
    if is_cycle_running():
        raise ValueError("Cannot delete while a pipeline cycle is running")

    with get_db() as db:
        row = mongo_query.find_row('bots', {'bot_id': bot_id}, ['is_active'])
        if not row:
            raise ValueError(f"Bot profile '{bot_id}' does not exist")
        if row[0]:
            raise ValueError(
                f"Cannot delete the active profile '{bot_id}'. "
                "Switch to a different profile first."
            )

        # Wipe all data for this bot
        tables_to_clear = [
            "positions",
            "orders",
            "trade_fills",
            "position_lots",
            "lot_closures",
            "portfolio_snapshots",
        ]
        for table in tables_to_clear:
            try:
                db.execute(f"DELETE FROM {table} WHERE bot_id = %s", [bot_id])
            except Exception as e:
                logger.warning("delete %s for %s: %s", table, bot_id, e)

        # Delete the bot row itself
        mongo_store.delete_docs('bots', {'bot_id': bot_id})
    logger.info("[BOT_MANAGER] Deleted profile: %s", bot_id)

    return {"bot_id": bot_id, "deleted": True}
