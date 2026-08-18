"""
Verdict Service — persistent, DB-backed swarm verdict access.

Provides deduplicated verdict views across cycles, unlike the ephemeral
cycleStatus.results which resets on every new cycle.
"""

import json
import logging
from datetime import datetime, timezone

from app.db import mongo_store

logger = logging.getLogger(__name__)


def get_latest_verdicts(limit: int = 100) -> list[dict]:
    """Return the most recent analysis verdict per ticker."""
    pipeline = [
        {"$sort": {"created_at": -1}},
        {"$group": {
            "_id": "$ticker",
            "doc": {"$first": "$$ROOT"}
        }},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"created_at": -1}},
        {"$limit": limit}
    ]
    try:
        docs = mongo_store.aggregate("analysis_results", pipeline)
    except Exception as e:
        logger.warning("[VerdictService] mongo aggregate failed: %s", e)
        docs = []

    tickers = [d.get("ticker") for d in docs if d.get("ticker")]
    notes = {}
    if tickers:
        try:
            note_docs = mongo_store.find_docs("ticker_user_notes", {"ticker": {"$in": tickers}})
            notes = {nd.get("ticker"): nd for nd in note_docs}
        except Exception:
            notes = {}

    verdicts = []
    for d in docs:
        result = _parse_result_json(d.get("result_json"))
        created_at = d.get("created_at")
        tk = d.get("ticker")
        note_doc = notes.get(tk, {})
        note_updated = note_doc.get("updated_at")
        verdicts.append({
            "ticker": tk,
            "action": result.get("action", "UNKNOWN"),
            "confidence": d.get("confidence"),
            "rationale": result.get("rationale", ""),
            "last_updated": created_at.isoformat() if hasattr(created_at, "isoformat") else (str(created_at) if created_at else None),
            "cycle_id": d.get("cycle_id"),
            "triage_tier": d.get("triage_tier"),
            "price_at_analysis": d.get("price_at_analysis"),
            "thesis_verdict": d.get("thesis_verdict"),
            "thesis_confidence": d.get("thesis_confidence"),
            "thesis_summary": d.get("thesis_summary"),
            "estimate": result.get("estimate", {}),
            "agent_results": result.get("agent_results", []),
            "user_note": note_doc.get("note"),
            "note_updated_at": note_updated.isoformat() if hasattr(note_updated, "isoformat") else (str(note_updated) if note_updated else None),
        })

    verdicts.sort(key=lambda v: v["last_updated"] or "", reverse=True)
    return verdicts


def get_verdict_history(ticker: str, limit: int = 20) -> list[dict]:
    """Return all verdicts for a specific ticker across cycles."""
    tk = ticker.upper().strip()
    try:
        docs = mongo_store.find_docs("analysis_results", {"ticker": tk}, sort=[("created_at", -1)], limit=limit)
    except Exception as e:
        logger.warning("[VerdictService] mongo find_docs failed: %s", e)
        docs = []

    history = []
    for d in docs:
        result = _parse_result_json(d.get("result_json"))
        created_at = d.get("created_at")
        history.append({
            "ticker": d.get("ticker"),
            "action": result.get("action", "UNKNOWN"),
            "confidence": d.get("confidence"),
            "rationale": result.get("rationale", ""),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else (str(created_at) if created_at else None),
            "cycle_id": d.get("cycle_id"),
            "triage_tier": d.get("triage_tier"),
            "price_at_analysis": d.get("price_at_analysis"),
            "thesis_verdict": d.get("thesis_verdict"),
            "thesis_confidence": d.get("thesis_confidence"),
            "thesis_summary": d.get("thesis_summary"),
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
