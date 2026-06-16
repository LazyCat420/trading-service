"""
Hiring Agent for the Civilization Council.
Handles replacing or demoting agents that go on cold streaks.
"""

import logging
import uuid
import json
import asyncio
from datetime import datetime, timezone
from app.db.mongo import get_mongo_db

logger = logging.getLogger(__name__)

def trigger_hiring_agent(role: str, consecutive_wrong: int, ticker: str = "SYSTEM", cycle_id: str = "SYSTEM"):
    """
    Trigger the hiring/replacement pipeline for a role that has reached the
    cold streak threshold.
    """
    logger.warning(f"⚠️ [HiringAgent] Firing replacement pipeline for {role} (Cold streak: {consecutive_wrong})")
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
            "action": "DEMOTED",
            "reason": f"Agent reached consecutive_wrong threshold ({consecutive_wrong}). Trust score reset to 0.8.",
        })
        
        # Spawn the async hiring pipeline
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(run_hiring_pipeline(role, consecutive_wrong, ticker, cycle_id))
            else:
                asyncio.run(run_hiring_pipeline(role, consecutive_wrong, ticker, cycle_id))
        except RuntimeError:
            asyncio.run(run_hiring_pipeline(role, consecutive_wrong, ticker, cycle_id))
            
    except Exception as e:
        logger.error(f"[HiringAgent] Failed to process hiring trigger for {role}: {e}")


async def run_hiring_pipeline(role: str, consecutive_wrong: int, ticker: str, cycle_id: str):
    """Orchestrates the 7-phase self-improving agent replacement pipeline."""
    try:
        # Phase 1: Post-Mortem
        post_mortem_data = await run_post_mortem(role, consecutive_wrong, ticker, cycle_id)
        
        # Phase 2: Tool Gaps
        recommended_tools = analyze_tool_gaps(role, post_mortem_data)
        
        # Phase 3: Prompt Generation
        new_prompt = await generate_replacement_prompt(role, post_mortem_data, recommended_tools)
        
        # Phase 4: Registration
        new_config = await register_new_agent(role, new_prompt, recommended_tools)
        
        # Phase 5: Hot-Swap
        await hotswap_agent(role, new_config)
        
    except Exception as e:
        logger.error(f"[HiringAgent] Error in hiring pipeline for {role}: {e}", exc_info=True)


async def run_post_mortem(role: str, consecutive_wrong: int, ticker: str, cycle_id: str) -> dict:
    """Phase 1: Query Mongo transcripts and SQL decision outcomes to identify agent failures."""
    db = get_mongo_db()
    
    # 1. Pull last 10 debate transcripts
    transcripts = list(db["debate_transcripts"].find(
        {f"manager_outcomes.{role}": {"$exists": True}}
    ).sort("timestamp", -1).limit(10))
    
    # 2. Pull last 10 challenge_log entries
    challenges = list(db["challenge_log"].find(
        {"challenged_agent_role": role}
    ).sort("timestamp", -1).limit(10))
    
    # 3. Pull last 10 resolved outcome_tracker records (decision_outcomes in PG)
    outcomes = []
    try:
        from app.db.connection import get_db
        with get_db() as pg_db:
            rows = pg_db.execute(
                "SELECT ticker, action, exit_price, pnl_pct, outcome, lesson_stored "
                "FROM decision_outcomes WHERE resolved_at IS NOT NULL "
                "ORDER BY resolved_at DESC LIMIT 10"
            ).fetchall()
            outcomes = [
                {
                    "ticker": r[0],
                    "action": r[1],
                    "exit_price": r[2],
                    "pnl_pct": r[3],
                    "outcome": r[4],
                    "lesson": r[5]
                }
                for r in rows
            ]
    except Exception as pg_err:
        logger.warning(f"[HiringAgent] Failed to query SQL decision_outcomes: {pg_err}")

    unverified_claims = []
    for c in challenges:
        if c.get("upheld", False):
            unverified_claims.append(c.get("claim", ""))
            
    post_mortem_id = f"pm-{uuid.uuid4().hex[:8]}"
    pm_doc = {
        "post_mortem_id": post_mortem_id,
        "role": role,
        "timestamp": datetime.now(timezone.utc),
        "consecutive_wrong": consecutive_wrong,
        "ticker": ticker,
        "cycle_id": cycle_id,
        "debate_history_count": len(transcripts),
        "challenges_raised_count": len(challenges),
        "unverified_claims": unverified_claims[:10],
        "outcome_history": outcomes
    }
    
    db["post_mortems"].insert_one(pm_doc)
    logger.info(f"[HiringAgent] Post-mortem {post_mortem_id} created for {role}")
    
    # Log post-mortem completion to nomination_history
    db["nomination_history"].insert_one({
        "event_id": f"nh-{uuid.uuid4().hex[:8]}",
        "timestamp": datetime.now(timezone.utc),
        "ticker": ticker,
        "cycle_id": cycle_id,
        "role": role,
        "action": "POST_MORTEM_COMPLETE",
        "reason": f"Post-mortem completed. ID: {post_mortem_id}",
    })
    
    return pm_doc


def analyze_tool_gaps(role: str, post_mortem: dict) -> list[str]:
    """Phase 2: Detect tools that the agent was lacking compared to registry capabilities."""
    from app.agents.debate_agents.family_office_managers import MANAGER_EVIDENCE_FILTER
    from app.tools.registry import registry
    
    current_whitelist = MANAGER_EVIDENCE_FILTER.get(role, [])
    all_registry_tools = [t["name"] for t in registry.get_registry_snapshot()]
    
    candidates = [t for t in all_registry_tools if t not in current_whitelist]
    recommended = []
    
    # Simple whitelist delta heuristic
    for c in candidates:
        if len(recommended) >= 3:
            break
        recommended.append(c)
        
    return recommended


async def generate_replacement_prompt(role: str, post_mortem: dict, new_tools: list[str]) -> str:
    """Phase 3: Meta-LLM prompt generation targeting weaknesses and tool whitelists."""
    from app.services.prism_agent_caller import call_prism_agent
    from app.agents.debate_agents.family_office_managers import MANAGER_PROMPTS
    
    current_prompt = MANAGER_PROMPTS.get(role, "")
    
    system_prompt = (
        "You are an AI Agent Architect designing an improved trading analyst agent. "
        "Your task is to analyze the performance post-mortem of a failing agent, and "
        "generate an improved system prompt keeping what worked and fixing the identified weaknesses."
    )
    
    user_message = (
        f"We are replacing the agent role: {role}.\n\n"
        f"Here is the failing agent's current system prompt:\n{current_prompt}\n\n"
        f"Here is the performance post-mortem:\n{json.dumps(post_mortem, default=str)}\n\n"
        f"Here are the new tools we plan to whitelist for the replacement agent:\n{new_tools}\n\n"
        "Generate a new, improved system prompt for the replacement agent. "
        "Keep the core analytical strength, persona characteristics, and formatted output rules, but update "
        "the prompt to address the tool gaps, prevent hallucinations (unverified claims), and correct biases. "
        "Return ONLY the new system prompt string."
    )
    
    response, _, _ = await call_prism_agent(
        agent_id="CUSTOM_HIRING_AGENT",
        user_message=user_message,
        fallback_system_prompt=system_prompt,
        fallback_agent_name="hiring_agent",
        max_tokens=8192,
        temperature=0.3
    )
    
    if len(response.strip()) < 100:
        logger.warning("[HiringAgent] LLM returned empty/short prompt. Falling back to current prompt.")
        return current_prompt
        
    return response.strip()


async def register_new_agent(role: str, new_prompt: str, new_tools: list[str]) -> dict:
    """Phase 4: Save agent configuration versioning to MongoDB."""
    db = get_mongo_db()
    col_configs = db["agent_configs"]
    
    latest = col_configs.find_one({"role": role}, sort=[("version", -1)])
    next_version = (latest.get("version", 0) + 1) if latest else 1
    
    new_config = {
        "role": role,
        "version": next_version,
        "system_prompt": new_prompt,
        "tool_whitelist": new_tools,
        "created_at": datetime.now(timezone.utc),
        "status": "PROBATION",
        "probation_cycles_remaining": 10,
        "vote_weight_cap": 0.7,
        "parent_version": latest.get("version", 0) if latest else 0,
        "post_mortem_id": "pm-latest"
    }
    
    col_configs.insert_one(new_config)
    logger.info(f"[HiringAgent] Registered new agent config for {role} (version {next_version})")
    
    db["nomination_history"].insert_one({
        "event_id": f"nh-{uuid.uuid4().hex[:8]}",
        "timestamp": datetime.now(timezone.utc),
        "ticker": "SYSTEM",
        "cycle_id": "SYSTEM",
        "role": role,
        "action": "NEW_AGENT_DESIGNED",
        "reason": f"New prompt and whitelist generated (Version: {next_version}). Status set to PROBATION.",
    })
    
    return new_config


async def hotswap_agent(role: str, new_config: dict):
    """Phase 5: Hot-swap agent system prompts and evidence filters in-memory."""
    import app.agents.debate_agents.family_office_managers as foms
    
    role_enum = None
    for r in foms.ManagerRole:
        if r.value == role:
            role_enum = r
            break
            
    if not role_enum:
        logger.error(f"[HiringAgent] Could not resolve ManagerRole for {role}")
        return
        
    new_prompt = new_config["system_prompt"]
    new_tools = new_config["tool_whitelist"]
    
    foms.MANAGER_PROMPTS[role_enum] = new_prompt
    
    if role_enum in foms.MANAGER_EVIDENCE_FILTER:
        foms.MANAGER_EVIDENCE_FILTER[role_enum] = list(set(foms.MANAGER_EVIDENCE_FILTER[role_enum] + new_tools))
    else:
        foms.MANAGER_EVIDENCE_FILTER[role_enum] = new_tools
        
    logger.warning(f"🔥 [HiringAgent] HOT-SWAPPED manager agent {role} in memory to version {new_config['version']}")
    
    db = get_mongo_db()
    db["nomination_history"].insert_one({
        "event_id": f"nh-{uuid.uuid4().hex[:8]}",
        "timestamp": datetime.now(timezone.utc),
        "ticker": "SYSTEM",
        "cycle_id": "SYSTEM",
        "role": role,
        "action": "ONBOARDING",
        "reason": f"Agent {role} hot-swapped and active in memory.",
    })


async def evaluate_probation(role: str) -> bool:
    """Phase 6: probation cycles countdown and graduation evaluation."""
    db = get_mongo_db()
    col_configs = db["agent_configs"]
    
    config = col_configs.find_one({"role": role, "status": "PROBATION"}, sort=[("version", -1)])
    if not config:
        return False
        
    cycles = config.get("probation_cycles_remaining", 10)
    new_cycles = max(0, cycles - 1)
    
    if new_cycles > 0:
        col_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"probation_cycles_remaining": new_cycles}}
        )
        logger.info(f"[HiringAgent] Agent {role} is on probation, {new_cycles} cycles remaining.")
        return False
        
    trust_scores = db["agent_trust_scores"].find_one({"role": role})
    win_rate = 0.5
    if trust_scores:
        history = trust_scores.get("history", [])
        probation_trades = history[-10:]
        wins = sum(1 for t in probation_trades if t.get("outcome") == "WIN")
        total = len(probation_trades)
        win_rate = (wins / total) if total > 0 else 0.5
        
    graduated = win_rate > 0.5
    
    if graduated:
        col_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"status": "ACTIVE", "probation_cycles_remaining": 0, "vote_weight_cap": 1.0}}
        )
        db["nomination_history"].insert_one({
            "event_id": f"nh-{uuid.uuid4().hex[:8]}",
            "timestamp": datetime.now(timezone.utc),
            "ticker": "SYSTEM",
            "cycle_id": "SYSTEM",
            "role": role,
            "action": "GRADUATED",
            "reason": f"Agent graduated from probation with win rate of {win_rate:.2%}.",
        })
        logger.warning(f"🎓 [HiringAgent] Agent {role} has GRADUATED from probation!")
        hiring_agent_self_score(True)
        return True
    else:
        col_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"status": "FIRED"}}
        )
        db["nomination_history"].insert_one({
            "event_id": f"nh-{uuid.uuid4().hex[:8]}",
            "timestamp": datetime.now(timezone.utc),
            "ticker": "SYSTEM",
            "cycle_id": "SYSTEM",
            "role": role,
            "action": "FIRED_AGAIN",
            "reason": f"Agent failed probation evaluation with win rate of {win_rate:.2%}.",
        })
        logger.warning(f"❌ [HiringAgent] Agent {role} failed probation and was FIRED again!")
        hiring_agent_self_score(False)
        trigger_hiring_agent(role, 5)
        return False


def hiring_agent_self_score(success: bool):
    """Phase 7: Hiring agent self-scoring algorithm based on placement outcomes."""
    try:
        db = get_mongo_db()
        col_scores = db["agent_trust_scores"]
        
        agent_doc = col_scores.find_one({"role": "HIRING_AGENT"})
        if not agent_doc:
            current_score = 0.8
        else:
            current_score = agent_doc.get("trust_score", 0.8)
            
        delta = 0.05 if success else -0.1
        new_score = max(0.1, min(1.0, current_score + delta))
        
        col_scores.update_one(
            {"role": "HIRING_AGENT"},
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
            },
            upsert=True
        )
        logger.info(f"[HiringAgent] Self-scoring updated: {current_score:.4f} -> {new_score:.4f} (success={success})")
    except Exception as e:
        logger.error(f"[HiringAgent] Failed self-scoring update: {e}")
