"""
Watchlist CRUD — PostgreSQL-backed ticker management with ban lifecycle.

Schema: watchlist(ticker, status, status_reason, banned_at, added_at, source, notes)
Status values: 'active' | 'paused' | 'removed' | 'banned'

Tables used:
  - watchlist       — main ticker list
  - ticker_bans     — permanent ban blocklist
  - ban_patterns    — learned auto-filter patterns

Usage:
    from app.trading.watchlist import (
        add_ticker, remove_ticker, get_active,
        ban_ticker, unban_ticker, is_banned, get_banned_list,
        pause_ticker, resume_ticker,
        import_from_discovery,
    )
"""

import json
import logging
from datetime import datetime, timezone

from app.db.connection import get_db
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


# ── Core CRUD ────────────────────────────────────────────────────────


def add_ticker(
    ticker: str,
    source: str = "manual",
    notes: str = "",
) -> bool:
    """Add a ticker to the watchlist. Returns True if newly added.

    Refuses to add if ticker is banned.
    Automatically resolves foreign tickers to US-listed equivalents.
    """
    ticker = ticker.upper().strip()

    # Gate: resolve foreign tickers to US equivalents
    from app.utils.us_ticker_resolver import is_us_tradeable, resolve_to_us_ticker
    if not is_us_tradeable(ticker):
        us_alt = resolve_to_us_ticker(ticker)
        if us_alt:
            logger.info("watchlist: resolved foreign ticker %s → %s", ticker, us_alt)
            ticker = us_alt
        else:
            logger.warning("watchlist: rejected non-US ticker %s (no US listing found)", ticker)
            return False

    # Gate: check ban list first
    if is_banned(ticker):
        logger.warning("watchlist: refused to add %s (banned)", ticker)
        return False

    with get_db() as db:
        existing = mongo_query.find_row('watchlist', {'ticker': ticker}, ['ticker', 'status'])
        if existing:
            mongo_store.update_docs('watchlist', {'ticker': ticker}, {'$set': {'status': 'active', 'status_reason': None, 'notes': notes, 'source': source}})
            logger.info("watchlist: reactivated %s", ticker)
            return False

        mongo_store.insert_docs('watchlist', [{'ticker': ticker, 'source': source, 'notes': notes, 'added_at': datetime.now(timezone.utc), 'status': 'active'}])
    logger.info("watchlist: added %s (source=%s)", ticker, source)
    return True


def remove_ticker(ticker: str) -> bool:
    """Soft-delete a ticker (status='removed'). Returns True if it existed."""
    ticker = ticker.upper().strip()
    with get_db() as db:
        row = mongo_query.find_row('watchlist', {'ticker': ticker, 'status': {'$in': ['active', 'paused']}}, ['ticker'])
        if not row:
            return False
        mongo_store.update_docs('watchlist', {'ticker': ticker}, {'$set': {'status': 'removed', 'status_reason': 'user removed'}})
    logger.info("watchlist: removed %s", ticker)
    return True


def auto_purge_ticker(ticker: str, reason: str = "") -> bool:
    """Auto-remove a ticker via the health purge system.

    Sets status='removed' with purge metadata.
    Different from ban — purged tickers can be re-added later.
    """
    ticker = ticker.upper().strip()
    now = datetime.now(timezone.utc)

    with get_db() as db:
        row = mongo_query.find_row('watchlist', {'ticker': ticker, 'status': {'$in': ['active', 'paused']}}, ['ticker'])
        if not row:
            return False

        mongo_store.update_docs('watchlist', {'ticker': ticker}, {'$set': {'status': 'removed', 'status_reason': f"auto_purge: {reason}", 'purged_at': now, 'purge_reason': reason}})
    logger.info("watchlist: AUTO-PURGED %s (reason: %s)", ticker, reason)
    return True


def get_active() -> list[dict]:
    """Return all active watchlist tickers with health scores."""
    with get_db() as db:
        rows = mongo_query.find_rows('watchlist', {'status': 'active'}, ['ticker', 'source', 'notes', 'added_at', 'health_score'], sort=[('added_at', -1)])
    return [
        {
            "ticker": r[0],
            "source": r[1],
            "notes": r[2],
            "added_at": r[3].isoformat() if r[3] else None,
            "health_score": r[4] if r[4] is not None else 50,
        }
        for r in rows
    ]


# ── Ban System ───────────────────────────────────────────────────────


def ban_ticker(ticker: str, reason: str = "") -> bool:
    """Permanently ban a ticker. Writes to ticker_bans and sets watchlist status.

    Returns True if newly banned, False if already banned.
    """
    ticker = ticker.upper().strip()
    now = datetime.now(timezone.utc)

    if is_banned(ticker):
        logger.info("watchlist: %s already banned", ticker)
        return False

    # Snapshot market data for pattern learning
    market_cap, price, volume = _snapshot_market_data(ticker)

    with get_db() as db:
        mongo_store.upsert_doc('ticker_bans', {'ticker': ticker}, {'ticker': ticker, 'reason': reason, 'ban_type': 'manual', 'market_cap': market_cap, 'price_at_ban': price, 'volume_at_ban': volume, 'banned_by': 'user', 'banned_at': now}, insert_only=True)

        # Update or insert watchlist entry
        existing = mongo_query.find_row('watchlist', {'ticker': ticker}, ['ticker'])
        if existing:
            mongo_store.update_docs('watchlist', {'ticker': ticker}, {'$set': {'status': 'banned', 'status_reason': reason, 'banned_at': now}})
        else:
            mongo_store.insert_docs('watchlist', [{'ticker': ticker, 'status': 'banned', 'status_reason': reason, 'banned_at': now, 'added_at': now, 'source': 'ban'}])

    logger.info("watchlist: BANNED %s (reason: %s)", ticker, reason)
    return True


def unban_ticker(ticker: str) -> bool:
    """Remove a ban. Ticker goes to 'removed' status."""
    ticker = ticker.upper().strip()

    if not is_banned(ticker):
        return False

    with get_db() as db:
        mongo_store.delete_docs('ticker_bans', {'ticker': ticker})
        mongo_store.update_docs('watchlist', {'ticker': ticker}, {'$set': {'status': 'removed', 'status_reason': 'unbanned', 'banned_at': None}})
    logger.info("watchlist: unbanned %s", ticker)
    return True


def is_banned(ticker: str) -> bool:
    """Fast lookup: is this ticker banned?"""
    ticker = ticker.upper().strip()
    try:
        with get_db() as db:
            row = mongo_query.find_row('ticker_bans', {'ticker': ticker}, ['ticker'])
            return row is not None
    except Exception as e:
        logger.error("watchlist: is_banned lookup failed for %s: %s", ticker, e)
        return False


def get_banned_list() -> list[dict]:
    """Return all banned tickers with reasons."""
    try:
        with get_db() as db:
            rows = mongo_query.find_rows('ticker_bans', {}, ['ticker', 'reason', 'ban_type', 'pattern_tags', 'market_cap', 'price_at_ban', 'volume_at_ban', 'banned_at'], sort=[('banned_at', -1)])
    except Exception as e:
        logger.error("watchlist: get_banned_list query failed: %s", e)
        return []
    return [
        {
            "ticker": r[0],
            "reason": r[1],
            "ban_type": r[2],
            "pattern_tags": r[3],
            "market_cap": r[4],
            "price_at_ban": r[5],
            "volume_at_ban": r[6],
            "banned_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


def check_ban_patterns(ticker: str) -> str | None:
    """Check if a ticker matches any active auto-filter ban pattern.

    Returns the pattern name if matched, None otherwise.
    """
    try:
        with get_db() as db:
            patterns = mongo_query.find_rows('ban_patterns', {'auto_filter': True}, ['pattern_name', 'conditions'])
    except Exception as e:
        logger.error("watchlist: check_ban_patterns query failed: %s", e)
        return None

    if not patterns:
        return None

    market_cap, price, volume = _snapshot_market_data(ticker)
    if price is None:
        return None

    for pattern_name, conditions_json in patterns:
        try:
            conds = json.loads(conditions_json) if conditions_json else {}
        except (json.JSONDecodeError, TypeError):
            continue

        if _matches_pattern(conds, market_cap, price, volume):
            logger.info("watchlist: %s matches ban pattern '%s'", ticker, pattern_name)
            return pattern_name

    return None


# ── Pause / Resume ───────────────────────────────────────────────────


def get_paused() -> list[dict]:
    """Return all paused watchlist tickers."""
    with get_db() as db:
        rows = mongo_query.find_rows('watchlist', {'status': 'paused'}, ['ticker', 'source', 'notes', 'added_at', 'status_reason'], sort=[('added_at', -1)])
    return [
        {
            "ticker": r[0],
            "source": r[1],
            "notes": r[2],
            "added_at": r[3].isoformat() if r[3] else None,
            "status_reason": r[4],
        }
        for r in rows
    ]


def pause_ticker(ticker: str, reason: str = "user paused") -> bool:
    """Temporarily pause a ticker. It won't be scraped this cycle."""
    ticker = ticker.upper().strip()
    with get_db() as db:
        row = mongo_query.find_row('watchlist', {'ticker': ticker, 'status': 'active'}, ['ticker'])
        if not row:
            return False
        mongo_store.update_docs('watchlist', {'ticker': ticker}, {'$set': {'status': 'paused', 'status_reason': reason}})
    logger.info("watchlist: paused %s", ticker)
    return True


def resume_ticker(ticker: str) -> bool:
    """Resume a paused ticker."""
    ticker = ticker.upper().strip()
    with get_db() as db:
        row = mongo_query.find_row('watchlist', {'ticker': ticker, 'status': 'paused'}, ['ticker'])
        if not row:
            return False
        mongo_store.update_docs('watchlist', {'ticker': ticker}, {'$set': {'status': 'active', 'status_reason': None}})
    logger.info("watchlist: resumed %s", ticker)
    return True


# ── Discovery Import ─────────────────────────────────────────────────


def import_from_discovery(min_score: float = 50.0) -> list[str]:
    """Import high-scoring tickers from discovered_tickers table.

    Skips banned tickers and pattern-matched tickers.
    """
    try:
        with get_db() as db:
            rows = mongo_query.find_rows('discovered_tickers', {'score': {'$gte': min_score}}, ['ticker'], sort=[('score', -1)])
    except Exception:
        logger.warning("discovered_tickers table not found")
        return []

    imported = []
    skipped_ban = []
    skipped_pattern = []

    for (ticker,) in rows:
        t = ticker.upper().strip()

        if is_banned(t):
            skipped_ban.append(t)
            continue

        pattern = check_ban_patterns(t)
        if pattern:
            skipped_pattern.append((t, pattern))
            continue

        if add_ticker(t, source="discovery"):
            imported.append(t)

    if skipped_ban:
        logger.info(
            "watchlist: skipped %d banned tickers: %s",
            len(skipped_ban),
            ", ".join(skipped_ban[:10]),
        )
    if skipped_pattern:
        logger.info(
            "watchlist: auto-filtered %d tickers by pattern: %s",
            len(skipped_pattern),
            ", ".join(f"{t}({p})" for t, p in skipped_pattern[:10]),
        )

    logger.info("watchlist: imported %d from discovery", len(imported))
    return imported


# ── Helpers ──────────────────────────────────────────────────────────


def _snapshot_market_data(ticker: str) -> tuple:
    """Get market cap, price, and volume from DB for pattern learning."""
    market_cap = None
    price = None
    volume = None

    with get_db() as db:
        try:
            fund_row = mongo_query.find_row('fundamentals', {'ticker': ticker}, ['market_cap'], sort=[('snapshot_date', -1)])
            if fund_row:
                market_cap = fund_row[0]
        except Exception as e:
            logger.warning("watchlist: _snapshot_market_data fundamentals lookup failed for %s: %s", ticker, e)

        try:
            price_row = mongo_query.find_row('price_history', {'ticker': ticker}, ['close', 'volume'], sort=[('date', -1)])
            if price_row:
                price = price_row[0]
                volume = price_row[1]
        except Exception as e:
            logger.warning("watchlist: _snapshot_market_data price_history lookup failed for %s: %s", ticker, e)

    return market_cap, price, volume


def _matches_pattern(
    conditions: dict,
    market_cap: float | None,
    price: float | None,
    volume: int | None,
) -> bool:
    """Check if market data matches ban pattern conditions (AND logic)."""
    checks = []

    if "price_lt" in conditions and price is not None:
        checks.append(price < conditions["price_lt"])
    if "price_gt" in conditions and price is not None:
        checks.append(price > conditions["price_gt"])
    if "volume_lt" in conditions and volume is not None:
        checks.append(volume < conditions["volume_lt"])
    if "market_cap_lt" in conditions and market_cap is not None:
        checks.append(market_cap < conditions["market_cap_lt"])
    if "market_cap_gt" in conditions and market_cap is not None:
        checks.append(market_cap > conditions["market_cap_gt"])

    return len(checks) > 0 and all(checks)
