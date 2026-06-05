"""
Verdict Service — persistent, DB-backed swarm verdict access.

Provides deduplicated verdict views across cycles, unlike the ephemeral
cycleStatus.results which resets on every new cycle.
"""

import json
import logging
from datetime import datetime, timezone

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def get_latest_verdicts(limit: int = 100) -> list[dict]:
    """Return the most recent analysis verdict per ticker.

    Uses DISTINCT ON to collapse multiple cycles into one row per ticker,
    keeping only the latest analysis. Joins ticker_user_notes for inline
    user context.
    """
    with get_db() as db:
        rows = db.execute(
            """
            SELECT DISTINCT ON (ar.ticker)
                ar.ticker,
                ar.result_json,
                ar.confidence,
                ar.created_at,
                ar.cycle_id,
                ar.triage_tier,
                ar.price_at_analysis,
                ar.thesis_verdict,
                ar.thesis_confidence,
                ar.thesis_summary,
                tun.note,
                tun.updated_at AS note_updated_at
            FROM analysis_results ar
            LEFT JOIN ticker_user_notes tun ON ar.ticker = tun.ticker
            ORDER BY ar.ticker, ar.created_at DESC
            LIMIT %s
            """,
            [limit],
        ).fetchall()

    verdicts = []
    for r in rows:
        result = _parse_result_json(r[1])
        verdicts.append({
            "ticker": r[0],
            "action": result.get("action", "UNKNOWN"),
            "confidence": r[2],
            "rationale": result.get("rationale", ""),
            "last_updated": r[3].isoformat() if r[3] else None,
            "cycle_id": r[4],
            "triage_tier": r[5],
            "price_at_analysis": r[6],
            "thesis_verdict": r[7],
            "thesis_confidence": r[8],
            "thesis_summary": r[9],
            "estimate": result.get("estimate", {}),
            "agent_results": result.get("agent_results", []),
            "user_note": r[10],
            "note_updated_at": r[11].isoformat() if r[11] else None,
        })

    # Sort by last_updated descending (most recent first)
    verdicts.sort(key=lambda v: v["last_updated"] or "", reverse=True)
    return verdicts


def get_verdict_history(ticker: str, limit: int = 20) -> list[dict]:
    """Return all verdicts for a specific ticker across cycles."""
    with get_db() as db:
        rows = db.execute(
            """
            SELECT
                ar.ticker,
                ar.result_json,
                ar.confidence,
                ar.created_at,
                ar.cycle_id,
                ar.triage_tier,
                ar.price_at_analysis,
                ar.thesis_verdict,
                ar.thesis_confidence,
                ar.thesis_summary
            FROM analysis_results ar
            WHERE ar.ticker = %s
            ORDER BY ar.created_at DESC
            LIMIT %s
            """,
            [ticker.upper().strip(), limit],
        ).fetchall()

    history = []
    for r in rows:
        result = _parse_result_json(r[1])
        history.append({
            "ticker": r[0],
            "action": result.get("action", "UNKNOWN"),
            "confidence": r[2],
            "rationale": result.get("rationale", ""),
            "created_at": r[3].isoformat() if r[3] else None,
            "cycle_id": r[4],
            "triage_tier": r[5],
            "price_at_analysis": r[6],
            "thesis_verdict": r[7],
            "thesis_confidence": r[8],
            "thesis_summary": r[9],
            "estimate": result.get("estimate", {}),
            "agent_results": result.get("agent_results", []),
        })
    return history


def _parse_result_json(raw) -> dict:
    """Safely parse result_json field."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
