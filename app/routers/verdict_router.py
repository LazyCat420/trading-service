"""
Verdict + User Notes Router — persistent swarm verdicts and per-ticker notes.

Endpoints:
  GET  /api/v1/verdicts/latest         — Latest verdict per ticker (persistent)
  GET  /api/v1/verdicts/history/:ticker — All verdicts for a specific ticker
  GET  /api/v1/notes                   — List all ticker notes
  GET  /api/v1/notes/:ticker           — Get note for a ticker
  PUT  /api/v1/notes/:ticker           — Upsert note for a ticker
  DELETE /api/v1/notes/:ticker         — Remove note for a ticker
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db.connection import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request Models ──


class TickerNoteUpsert(BaseModel):
    """Create or update a user note on a ticker."""
    note: str


# ── Verdict Endpoints ──


@router.get("/api/v1/verdicts/latest")
def verdicts_latest(limit: int = Query(default=100, le=500)):
    """Latest debate verdict per ticker — persistent DB-backed view."""
    from app.services.debate_service import get_latest_debates

    try:
        return get_latest_debates(limit=limit)
    except Exception as e:
        logger.exception("Error in /verdicts/latest")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/verdicts/history/{ticker}")
def verdicts_history(ticker: str, limit: int = Query(default=20, le=100)):
    """All verdicts for a specific ticker across cycles."""
    from app.services.verdict_service import get_verdict_history

    try:
        return get_verdict_history(ticker=ticker, limit=limit)
    except Exception as e:
        logger.exception("Error in /verdicts/history/%s", ticker)
        raise HTTPException(status_code=500, detail=str(e))


# ── Ticker User Notes Endpoints ──


@router.get("/api/v1/ticker-notes")
def list_ticker_notes():
    """List all ticker notes."""
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker, note, updated_at FROM ticker_user_notes ORDER BY updated_at DESC"
        ).fetchall()
    return [
        {
            "ticker": r[0],
            "note": r[1],
            "updated_at": r[2].isoformat() if r[2] else None,
        }
        for r in rows
    ]


@router.get("/api/v1/ticker-notes/{ticker}")
def get_ticker_note(ticker: str):
    """Get user note for a specific ticker."""
    with get_db() as db:
        row = db.execute(
            "SELECT ticker, note, updated_at FROM ticker_user_notes WHERE ticker = %s",
            [ticker.upper().strip()],
        ).fetchone()
    if not row:
        return {"ticker": ticker.upper().strip(), "note": None, "updated_at": None}
    return {
        "ticker": row[0],
        "note": row[1],
        "updated_at": row[2].isoformat() if row[2] else None,
    }


@router.put("/api/v1/ticker-notes/{ticker}")
def upsert_ticker_note(ticker: str, body: TickerNoteUpsert):
    """Create or update a user note for a ticker."""
    ticker_clean = ticker.upper().strip()
    now = datetime.now(timezone.utc)

    if not body.note or not body.note.strip():
        raise HTTPException(400, "Note cannot be empty")

    with get_db() as db:
        db.execute(
            """
            INSERT INTO ticker_user_notes (ticker, note, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (ticker) DO UPDATE
            SET note = EXCLUDED.note, updated_at = EXCLUDED.updated_at
            """,
            [ticker_clean, body.note.strip(), now],
        )
    logger.info("ticker note upserted: %s", ticker_clean)
    return {"ticker": ticker_clean, "note": body.note.strip(), "updated_at": now.isoformat(), "saved": True}


@router.delete("/api/v1/ticker-notes/{ticker}")
def delete_ticker_note(ticker: str):
    """Remove a user note for a ticker."""
    ticker_clean = ticker.upper().strip()
    with get_db() as db:
        row = db.execute(
            "SELECT ticker FROM ticker_user_notes WHERE ticker = %s",
            [ticker_clean],
        ).fetchone()
        if not row:
            raise HTTPException(404, f"No note found for {ticker_clean}")
        db.execute(
            "DELETE FROM ticker_user_notes WHERE ticker = %s",
            [ticker_clean],
        )
    logger.info("ticker note deleted: %s", ticker_clean)
    return {"ticker": ticker_clean, "deleted": True}
