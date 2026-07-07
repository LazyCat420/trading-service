"""
Desk Persistence — Postgres persistence for SharedDesk.

Uses the existing get_db() connection pool. Creates the shared_desk
table on first use (idempotent). Stores desk state as JSONB for
flexibility — the schema evolves with the artifacts.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.v3.shared_desk import SharedDesk

logger = logging.getLogger(__name__)

_TABLE_ENSURED = False


def _ensure_table() -> None:
    """Create the shared_desk table if it doesn't exist (idempotent)."""
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return

    from app.db.connection import get_db

    try:
        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS shared_desk (
                    desk_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'INIT',
                    desk_data JSONB NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            # Index for fast lookups by cycle_id + ticker
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_shared_desk_cycle_ticker
                ON shared_desk (cycle_id, ticker)
            """)
            # Index for listing all desks in a cycle
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_shared_desk_cycle
                ON shared_desk (cycle_id)
            """)
        _TABLE_ENSURED = True
        logger.debug("[DeskPersistence] Table shared_desk ensured")
    except Exception as e:
        logger.warning("[DeskPersistence] Failed to ensure table: %s", e)


def save_desk(desk: SharedDesk) -> None:
    """Upsert a SharedDesk to Postgres.

    Uses INSERT ... ON CONFLICT to handle both create and update.
    The entire desk state is serialized as JSONB in desk_data.
    """
    _ensure_table()
    from app.db.connection import get_db

    desk_data = json.dumps(desk.to_dict(), default=str)

    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO shared_desk (desk_id, cycle_id, ticker, phase, desk_data, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (desk_id) DO UPDATE SET
                    phase = EXCLUDED.phase,
                    desk_data = EXCLUDED.desk_data,
                    updated_at = NOW()
                """,
                [desk.desk_id, desk.cycle_id, desk.ticker, desk.phase.value, desk_data],
            )
        logger.debug(
            "[DeskPersistence] Saved desk %s/%s (phase=%s)",
            desk.cycle_id[:12] if desk.cycle_id else "?",
            desk.ticker,
            desk.phase.value,
        )
    except Exception as e:
        logger.error("[DeskPersistence] Failed to save desk: %s", e)
        raise


def load_desk(cycle_id: str, ticker: str) -> SharedDesk | None:
    """Load a SharedDesk from Postgres by cycle_id + ticker.

    Returns None if no desk exists for this combination.
    """
    _ensure_table()
    from app.db.connection import get_db

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT desk_data FROM shared_desk WHERE cycle_id = %s AND ticker = %s",
                [cycle_id, ticker.upper()],
            ).fetchone()

        if not row:
            return None

        raw = row[0]
        if isinstance(raw, str):
            data = json.loads(raw)
        else:
            data = raw  # Already parsed by psycopg2/JSONB

        return SharedDesk.from_dict(data)
    except Exception as e:
        logger.error(
            "[DeskPersistence] Failed to load desk %s/%s: %s",
            cycle_id[:12], ticker, e,
        )
        return None


def list_desks(cycle_id: str) -> list[SharedDesk]:
    """List all SharedDesks for a given cycle."""
    _ensure_table()
    from app.db.connection import get_db

    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT desk_data FROM shared_desk WHERE cycle_id = %s ORDER BY created_at",
                [cycle_id],
            ).fetchall()

        desks = []
        for (raw,) in rows:
            if isinstance(raw, str):
                data = json.loads(raw)
            else:
                data = raw
            desks.append(SharedDesk.from_dict(data))
        return desks
    except Exception as e:
        logger.error(
            "[DeskPersistence] Failed to list desks for cycle %s: %s",
            cycle_id[:12], e,
        )
        return []


def delete_desk(desk_id: str) -> bool:
    """Delete a SharedDesk by desk_id. Returns True if deleted."""
    _ensure_table()
    from app.db.connection import get_db

    try:
        with get_db() as db:
            result = db.execute(
                "DELETE FROM shared_desk WHERE desk_id = %s",
                [desk_id],
            )
            deleted = result.rowcount > 0 if hasattr(result, "rowcount") else True
        return deleted
    except Exception as e:
        logger.error("[DeskPersistence] Failed to delete desk %s: %s", desk_id, e)
        return False


def load_latest_desk_for_ticker(ticker: str) -> SharedDesk | None:
    """Load the most recent SharedDesk for a given ticker, regardless of cycle_id."""
    _ensure_table()
    from app.db.connection import get_db

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT desk_data FROM shared_desk WHERE ticker = %s ORDER BY created_at DESC LIMIT 1",
                [ticker.upper()],
            ).fetchone()

        if not row:
            return None

        raw = row[0]
        if isinstance(raw, str):
            data = json.loads(raw)
        else:
            data = raw

        return SharedDesk.from_dict(data)
    except Exception as e:
        logger.error(
            "[DeskPersistence] Failed to load latest desk for ticker %s: %s",
            ticker, e,
        )
        return None
