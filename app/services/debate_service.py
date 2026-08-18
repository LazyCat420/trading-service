"""
Debate Service — persistent, DB-backed rich debate access.

Provides the Civilization Council Debate reports.
"""

import json
import logging
from app.db.connection import get_db

logger = logging.getLogger(__name__)

def get_latest_debates(limit: int = 100) -> list[dict]:
    """Return the most recent debate report per ticker."""
    with get_db() as db:
        rows = db.execute(
            """
            WITH LatestDebates AS (
                SELECT DISTINCT ON (ar.ticker)
                    ar.ticker,
                    ar.result_json,
                    ar.created_at,
                    ar.cycle_id,
                    tun.note
                FROM analysis_results ar
                LEFT JOIN ticker_user_notes tun ON ar.ticker = tun.ticker
                ORDER BY ar.ticker, ar.created_at DESC
            )
            SELECT * FROM LatestDebates
            ORDER BY created_at DESC
            LIMIT %s
            """,
            [limit],
        ).fetchall()

    debates = []
    for r in rows:
        result = _parse_result_json(r[1])
        # Format this into the new Civilization Council report format
        debates.append({
            "ticker": r[0],
            "cio_verdict": result.get("action", "UNKNOWN"),
            "cio_confidence": result.get("confidence", 0),
            "cio_rationale": result.get("rationale", ""),
            "created_at": r[2].isoformat() if r[2] else None,
            "cycle_id": r[3],
            "user_note": r[4],
            "council_votes": _extract_council_votes(result),
            "transcript": _extract_transcript(result)
        })

    debates.sort(key=lambda v: v["created_at"] or "", reverse=True)
    return debates

def _extract_council_votes(result: dict) -> list[dict]:
    """Extract individual archetype votes from the new debate structure."""
    agent_results = result.get("agent_results", [])
    votes = []
    
    if isinstance(agent_results, dict):
        for role, agent in agent_results.items():
            if isinstance(agent, dict):
                votes.append({
                    "archetype": agent.get("archetype", role.replace("_", " ").title()),
                    "agent_id": agent.get("agent_id", role),
                    "vote": agent.get("action", agent.get("vote", "UNKNOWN")),
                    "confidence": agent.get("confidence", 0),
                    "rationale": agent.get("rationale", agent.get("response", "")),
                    "metrics": agent.get("metrics", {})
                })
            elif isinstance(agent, str):
                votes.append({
                    "archetype": role.replace("_", " ").title(),
                    "agent_id": role,
                    "vote": "UNKNOWN",
                    "confidence": 0,
                    "rationale": agent,
                    "metrics": {}
                })
    elif isinstance(agent_results, list):
        for agent in agent_results:
            if isinstance(agent, dict):
                votes.append({
                    "archetype": agent.get("archetype", "UNKNOWN"),
                    "agent_id": agent.get("agent_id", "UNKNOWN"),
                    "vote": agent.get("action", "HOLD"),
                    "confidence": agent.get("confidence", 0),
                    "rationale": agent.get("rationale", ""),
                    "metrics": agent.get("metrics", {})
                })
    return votes

def _extract_transcript(result: dict) -> list[dict]:
    """Extract the adversarial debate transcript if present."""
    # Assuming debate_result was saved in result_json under 'debate_transcript'
    return result.get("debate_transcript", [])

def _parse_result_json(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}
