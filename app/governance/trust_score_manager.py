"""
Trust Score Manager for the Civilization Council.
Manages persistent trust scores in MongoDB, tracks accuracy, and calculates vote weights.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from app.db.mongo import get_mongo_db
from app.governance.hiring_agent import trigger_hiring_agent

logger = logging.getLogger(__name__)

# Conviction multipliers from debate.md
CONVICTION_MULTIPLIERS = {
    "EXTREME": 2.0,
    "HIGH": 1.5,
    "MODERATE": 1.0,
    "LOW": 0.5,
    "WATCH": 0.1
}

def get_agent_trust_score(role: str) -> float:
    """Get the current trust score for an agent role. Defaults to 1.0 if not set."""
    try:
        db = get_mongo_db()
        doc = db["agent_trust_scores"].find_one({"role": role})
        if doc:
            return doc.get("trust_score", 1.0)
    except Exception as e:
        logger.error(f"[TrustScore] Failed to get trust score for {role}: {e}")
    return 1.0

def get_all_trust_scores() -> Dict[str, float]:
    """Retrieve all agent trust scores from MongoDB."""
    scores = {}
    try:
        db = get_mongo_db()
        for doc in db["agent_trust_scores"].find({"role": {"$ne": None}}):
            scores[doc["role"]] = doc.get("trust_score", 1.0)
    except Exception as e:
        logger.error(f"[TrustScore] Failed to get all trust scores: {e}")
    return scores

def update_trust_scores_on_outcome(ticker: str, cycle_id: str, action: str, outcome: str, pnl_pct: float):
    """
    Update agent trust scores based on a resolved trade outcome.
    Fired by the Outcome Tracker when exit_price / outcome is determined.
    """
    logger.info(f"[TrustScore] Updating trust scores for {ticker} (cycle={cycle_id}, action={action}, outcome={outcome}, pnl={pnl_pct:.2f}%)")
    try:
        db = get_mongo_db()
        
        # 1. Fetch the debate transcript to see manager stances/direction
        transcript = db["debate_transcripts"].find_one({"ticker": ticker, "cycle_id": cycle_id})
        if not transcript:
            logger.warning(f"[TrustScore] No debate transcript found for {ticker} in cycle {cycle_id}. Cannot update trust scores.")
            return

        manager_outcomes = transcript.get("manager_outcomes", {})
        if not manager_outcomes:
            logger.warning(f"[TrustScore] No manager outcomes in transcript for {ticker} in cycle {cycle_id}.")
            return

        col_scores = db["agent_trust_scores"]
        
        # 2. Iterate through each manager's contribution
        for role, data in manager_outcomes.items():
            direction = data.get("direction", "neutral")
            confidence = data.get("confidence", 0)
            conviction = data.get("conviction", "MODERATE")
            
            if direction not in ("bull", "bear"):
                # Neutral votes do not affect trust scores
                continue

            # Load current score (initialize if missing)
            agent_doc = col_scores.find_one({"role": role})
            if not agent_doc:
                agent_doc = {
                    "role": role,
                    "trust_score": 1.0,
                    "consecutive_correct": 0,
                    "consecutive_wrong": 0,
                    "challenges_raised": 0,
                    "challenges_upheld": 0,
                    "history": []
                }
            
            current_score = agent_doc.get("trust_score", 1.0)
            consecutive_correct = agent_doc.get("consecutive_correct", 0)
            consecutive_wrong = agent_doc.get("consecutive_wrong", 0)
            
            # Calculate delta based on outcome and conviction
            confidence_ratio = confidence / 100.0
            mult = CONVICTION_MULTIPLIERS.get(conviction.upper(), 1.0)
            delta = 0.0

            if outcome == "WIN":
                # matching trade action
                if (direction == "bull" and action == "BUY") or (direction == "bear" and action == "SELL"):
                    delta = 0.05 * confidence_ratio * mult
                # opposite to trade action
                else:
                    delta = -0.08 * confidence_ratio * mult
            elif outcome == "LOSS":
                # matching trade action (incorrect warning/support)
                if (direction == "bull" and action == "BUY") or (direction == "bear" and action == "SELL"):
                    delta = -0.05 * confidence_ratio
                # opposite to trade action (correctly warned against)
                else:
                    delta = 0.05 * confidence_ratio

            if delta == 0.0:
                continue

            new_score = max(0.1, min(1.0, current_score + delta))
            
            # Update streaks
            if delta > 0:
                consecutive_correct += 1
                consecutive_wrong = 0
            else:
                consecutive_wrong += 1
                consecutive_correct = 0
            
            # Record change history
            history_entry = {
                "timestamp": datetime.now(timezone.utc),
                "ticker": ticker,
                "cycle_id": cycle_id,
                "trade_action": action,
                "outcome": outcome,
                "pnl_pct": pnl_pct,
                "agent_direction": direction,
                "agent_confidence": confidence,
                "agent_conviction": conviction,
                "delta": delta,
                "old_score": current_score,
                "new_score": new_score
            }
            
            col_scores.update_one(
                {"role": role},
                {
                    "$set": {
                        "trust_score": round(new_score, 4),
                        "consecutive_correct": consecutive_correct,
                        "consecutive_wrong": consecutive_wrong,
                        "last_updated": datetime.now(timezone.utc)
                    },
                    "$push": {
                        "history": {
                            "$each": [history_entry],
                            "$slice": -100 # keep last 100 history entries
                        },
                        "score_history": {
                            "$each": [{"timestamp": datetime.now(timezone.utc), "score": round(new_score, 4)}],
                            "$slice": -50 # keep last 50 score history entries
                        }
                    }
                },
                upsert=True
            )
            
            logger.info(f"[TrustScore] {role}: {current_score:.4f} -> {new_score:.4f} (cc={consecutive_correct}, cw={consecutive_wrong})")
            
            # Trigger Cold Streak Watchdog warnings if streak >= 5
            if consecutive_wrong >= 5:
                logger.warning(f"⚠️ [WATCHDOG] Agent {role} is on a COLD STREAK of {consecutive_wrong} consecutive wrong calls! Demotion/Hiring triggered.")
                trigger_hiring_agent(role, consecutive_wrong, ticker, cycle_id)
                 
        # 3. Process dissent accuracy tracking
        try:
            dissent_cursor = db["dissent_log"].find({"ticker": ticker, "cycle_id": cycle_id})
            for dissent in dissent_cursor:
                role = dissent.get("manager_role")
                if not role:
                    continue
                # If the verdict lost money, the dissenter was right!
                if outcome == "LOSS":
                    agent_doc = col_scores.find_one({"role": role})
                    if agent_doc:
                        current_score = agent_doc.get("trust_score", 1.0)
                        # Reward bonus +0.02
                        new_score = min(1.0, current_score + 0.02)
                        
                        col_scores.update_one(
                            {"role": role},
                            {
                                "$set": {
                                    "trust_score": round(new_score, 4),
                                    "last_updated": datetime.now(timezone.utc)
                                },
                                "$push": {
                                    "score_history": {
                                        "$each": [{"timestamp": datetime.now(timezone.utc), "score": round(new_score, 4)}],
                                        "$slice": -50
                                    }
                                }
                            }
                        )
                        logger.info(f"[TrustScore] Dissenter {role} rewarded: {current_score:.4f} -> {new_score:.4f} (verdict was wrong on {ticker})")
        except Exception as dissent_err:
            logger.error(f"[TrustScore] Failed to reconcile dissenter accuracy: {dissent_err}")
                
    except Exception as e:
        logger.error(f"[TrustScore] Failed to update trust scores on outcome: {e}", exc_info=True)


def update_trust_scores_from_debate(rnd: Any, ticker: str, cycle_id: str):
    """
    Apply intermediate, fractional trust score deltas immediately after a debate round based on performance.
    """
    try:
        db = get_mongo_db()
        col_scores = db["agent_trust_scores"]
        
        # We assume rnd is a DebateRound object
        if not hasattr(rnd, "pm_arguments") or not rnd.pm_arguments:
            logger.warning("[TrustScore] Debate round missing pm_arguments — skipping interim delta")
            return
            
        conviction_weights = {"EXTREME": 2.0, "HIGH": 1.5, "MODERATE": 1.0, "LOW": 0.5, "WATCH": 0.25}

        for arg in rnd.pm_arguments:
            role = arg.role.value if hasattr(arg.role, "value") else str(arg.role)
            conv_str = arg.conviction.upper() if getattr(arg, "conviction", None) else "MODERATE"
            weight = conviction_weights.get(conv_str, 1.0)
            delta = 0.002 * weight * min(len(arg.claims), 5) if len(arg.claims) > 0 else -0.005
            
            agent_doc = col_scores.find_one({"role": role})
            if not agent_doc:
                # Seed with default trust score 0.8
                current_score = 0.8
            else:
                current_score = agent_doc.get("trust_score", 0.8)
                
            new_score = max(0.1, min(1.0, current_score + delta))
            
            col_scores.update_one(
                {"role": role},
                {
                    "$set": {"trust_score": round(new_score, 4), "last_updated": datetime.now(timezone.utc)},
                    "$push": {
                        "score_history": {
                            "$each": [{"timestamp": datetime.now(timezone.utc), "score": round(new_score, 4)}],
                            "$slice": -50
                        }
                    }
                },
                upsert=True
            )
    except Exception as e:
        logger.error(f"[TrustScore] Failed to apply interim debate trust score deltas: {e}", exc_info=True)


def resolve_challenges_and_update_trust(ticker: str, cycle_id: str):
    """
    Query challenge_log for the given ticker and cycle_id, and update
    challenges_raised and challenges_upheld counters for each agent.
    """
    try:
        db = get_mongo_db()
        col_scores = db["agent_trust_scores"]
        
        # Aggregate challenges by challenged_agent_role
        challenges = db["challenge_log"].find({"ticker": ticker, "cycle_id": cycle_id})
        
        updates = {} # role -> {raised: int, upheld: int}
        for c in challenges:
            role = c.get("challenged_agent_role")
            if not role or role == "unknown":
                continue
            if role not in updates:
                updates[role] = {"raised": 0, "upheld": 0}
            updates[role]["raised"] += 1
            if c.get("upheld", False):
                updates[role]["upheld"] += 1
                
        for role, counts in updates.items():
            col_scores.update_one(
                {"role": role},
                {
                    "$inc": {
                        "challenges_raised": counts["raised"],
                        "challenges_upheld": counts["upheld"]
                    }
                },
                upsert=True
            )
            logger.info(f"[TrustScore] Resolved challenges for {role} (cycle={cycle_id}): raised={counts['raised']}, upheld={counts['upheld']}")
            
    except Exception as e:
        logger.error(f"[TrustScore] Failed to resolve challenges and update trust: {e}", exc_info=True)

