"""
Debate Service — persistent, DB-backed rich debate access.

Provides the Civilization Council Debate reports.
"""

import logging
from app.db import mongo_store
from app.utils.json_utils import parse_json_field as _parse_result_json

logger = logging.getLogger(__name__)

def get_latest_debates(limit: int = 100) -> list[dict]:
    """Return the most recent debate report per ticker."""
    # DISTINCT ON (ticker) ordered by created_at DESC == group by ticker keeping
    # the newest doc; the LEFT JOIN onto ticker_user_notes becomes a $lookup
    # that yields note=None when the ticker has no note.
    docs = mongo_store.aggregate(
        'analysis_results',
        [
            {'$sort': {'ticker': 1, 'created_at': -1}},
            {'$group': {'_id': '$ticker', 'doc': {'$first': '$$ROOT'}}},
            {'$replaceRoot': {'newRoot': '$doc'}},
            {'$lookup': {
                'from': 'ticker_user_notes',
                'localField': 'ticker',
                'foreignField': 'ticker',
                'as': '_notes',
            }},
            {'$sort': {'created_at': -1}},
            {'$limit': int(limit)},
            {'$project': {
                '_id': 0,
                'ticker': 1,
                'result_json': 1,
                'created_at': 1,
                'cycle_id': 1,
                'note': {'$first': '$_notes.note'},
            }},
        ],
    )
    rows = [
        (d.get('ticker'), d.get('result_json'), d.get('created_at'),
         d.get('cycle_id'), d.get('note'))
        for d in docs
    ]

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

