"""
Hiring Agent for the Civilization Council.
Handles replacing or demoting agents that go on cold streaks.
"""

import logging
import uuid
from datetime import datetime, timezone
from app.db.mongo import get_mongo_db

logger = logging.getLogger(__name__)

def trigger_hiring_agent(role: str, consecutive_wrong: int, ticker: str = "SYSTEM", cycle_id: str = "SYSTEM"):
    """
    Trigger the hiring/replacement pipeline for a role that has reached the
    cold streak threshold. Currently this acts as a stub that sets the agent 
    to probation status and logs the demotion to nomination_history.
    """
    logger.info(f"[HiringAgent] Firing replacement pipeline for {role} (Cold streak: {consecutive_wrong})")
    try:
        db = get_mongo_db()
        col_scores = db["agent_trust_scores"]
        
        # Reset the streak and drop to probation score
        col_scores.update_one(
            {"role": role},
            {
                "$set": {
                    "trust_score": 0.8,
                    "consecutive_correct": 0,
                    "consecutive_wrong": 0,
                    "last_updated": datetime.now(timezone.utc)
                }
            }
        )
        
        # Log this event in nomination_history
        db["nomination_history"].insert_one({
            "event_id": f"nh-{uuid.uuid4().hex[:8]}",
            "timestamp": datetime.now(timezone.utc),
            "ticker": ticker,
            "cycle_id": cycle_id,
            "role": role,
            "action": "DEMOTION_PROBATION",
            "reason": f"Agent reached consecutive_wrong threshold ({consecutive_wrong}). Trust score reset to 0.8.",
        })
        
        logger.info(f"[HiringAgent] {role} has been placed on probation.")
    except Exception as e:
        logger.error(f"[HiringAgent] Failed to process hiring trigger for {role}: {e}")
