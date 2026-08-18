"""
Desk Persistence — Pure MongoDB persistence for SharedDesk.

Stores desk state in the shared_desk collection.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from datetime import datetime, timezone

from app.v3.shared_desk import SharedDesk
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


def save_desk(desk: SharedDesk) -> None:
    """Upsert a SharedDesk to MongoDB."""
    try:
        from app.v3.telemetry import flush_agent_telemetry
        flush_agent_telemetry(desk)
    except Exception as e:  # noqa: BLE001 — never let cost accounting lose a desk
        logger.debug("[DeskPersistence] telemetry flush skipped (non-fatal): %s", e)

    desk_data = json.dumps(desk.to_dict(), default=str)
    now_utc = datetime.now(timezone.utc)

    try:
        mongo_store.upsert_doc(
            'shared_desk',
            {'cycle_id': desk.cycle_id, 'ticker': desk.ticker.upper()},
            {
                'desk_id': desk.desk_id,
                'cycle_id': desk.cycle_id,
                'ticker': desk.ticker.upper(),
                'phase': desk.phase.value,
                'desk_data': desk_data,
                'updated_at': now_utc,
                'created_at': now_utc,
            },
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
    """Load a SharedDesk from MongoDB by cycle_id + ticker."""
    try:
        row = mongo_query.find_row(
            'shared_desk',
            {'cycle_id': cycle_id, 'ticker': ticker.upper()},
            ['desk_data'],
        )
        if not row or not row[0]:
            return None

        raw = row[0]
        data = json.loads(raw) if isinstance(raw, str) else raw
        return SharedDesk.from_dict(data)
    except Exception as e:
        logger.error(
            "[DeskPersistence] Failed to load desk %s/%s: %s",
            cycle_id[:12], ticker, e,
        )
        return None


def list_desks(cycle_id: str) -> list[SharedDesk]:
    """List all SharedDesks for a given cycle."""
    try:
        rows = mongo_query.find_rows(
            'shared_desk',
            {'cycle_id': cycle_id},
            ['desk_data'],
            sort=[('created_at', 1)],
        )
        desks = []
        for (raw,) in rows:
            if not raw:
                continue
            data = json.loads(raw) if isinstance(raw, str) else raw
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
    try:
        deleted_count = mongo_store.delete_docs('shared_desk', {'desk_id': desk_id})
        return bool(deleted_count)
    except Exception as e:
        logger.error("[DeskPersistence] Failed to delete desk %s: %s", desk_id, e)
        return False


def load_latest_desk_for_ticker(ticker: str) -> SharedDesk | None:
    """Load the most recent SharedDesk for a given ticker, regardless of cycle_id."""
    try:
        row = mongo_query.find_row(
            'shared_desk',
            {'ticker': ticker.upper()},
            ['desk_data'],
            sort=[('created_at', -1)],
        )
        if not row or not row[0]:
            return None

        raw = row[0]
        data = json.loads(raw) if isinstance(raw, str) else raw
        return SharedDesk.from_dict(data)
    except Exception as e:
        logger.error(
            "[DeskPersistence] Failed to load latest desk for ticker %s: %s",
            ticker, e,
        )
        return None
