"""
agent_persona_router.py — CRUD API for Agent Studio persona management.

Endpoints:
  GET    /api/v1/agents             — list all personas
  POST   /api/v1/agents             — create a new persona
  GET    /api/v1/agents/{id}        — get single persona
  PUT    /api/v1/agents/{id}        — update a persona
  DELETE /api/v1/agents/{id}        — delete a persona
  POST   /api/v1/agents/reset-defaults — re-seed from hardcoded defaults
"""

import logging
from fastapi import APIRouter, HTTPException
from app.schemas.agent_persona import AgentPersonaCreate, AgentPersonaUpdate
from app.db import agent_persona_store as store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["agent-studio"])


@router.get("")
async def list_personas():
    """List all agent personas, sorted by execution_order."""
    personas = await store.list_personas()
    return {"agents": personas, "count": len(personas)}


@router.post("")
async def create_persona(body: AgentPersonaCreate):
    """Create a new agent persona."""
    data = body.model_dump()
    # Convert AvatarConfig model to dict if present
    if data.get("avatar_config") is not None:
        data["avatar_config"] = (
            data["avatar_config"]
            if isinstance(data["avatar_config"], dict)
            else data["avatar_config"]
        )
    persona = await store.create_persona(data)
    return persona


@router.get("/reset-defaults")
async def _block_get_reset():
    """Prevent accidental GET on the reset endpoint."""
    raise HTTPException(status_code=405, detail="Use POST to reset defaults")


@router.post("/reset-defaults")
async def reset_defaults():
    """Re-seed all personas from hardcoded defaults (destructive)."""
    personas = await store.reset_to_defaults()
    return {"agents": personas, "count": len(personas), "status": "reset_complete"}


@router.get("/trust-scores")
async def get_trust_scores():
    """Retrieve current trust scores for all Civilization Council agent personas."""
    try:
        from app.governance.trust_score_manager import get_all_trust_scores
        scores = get_all_trust_scores()
        return {"scores": scores, "status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch trust scores: {e}")


@router.get("/debate-transcripts")
async def get_debate_transcripts(ticker: str = None, cycle_id: str = None):
    """Retrieve debate transcripts for Civilization Council debates."""
    try:
        from app.db.mongo import get_mongo_db
        db = get_mongo_db()
        query = {}
        if ticker:
            query["ticker"] = ticker.upper().strip()
        if cycle_id:
            query["cycle_id"] = cycle_id
        
        # Exclude _id to make it JSON serializable easily
        cursor = db["debate_transcripts"].find(query, {"_id": 0}).sort("timestamp", -1).limit(50)
        transcripts = list(cursor)
        return {"transcripts": transcripts, "status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch debate transcripts: {e}")


@router.get("/{persona_id}")
async def get_persona(persona_id: str):
    """Get a single persona by ID."""
    persona = await store.get_persona(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail=f"Persona '{persona_id}' not found")
    return persona


@router.put("/{persona_id}")
async def update_persona(persona_id: str, body: AgentPersonaUpdate):
    """Update an existing persona (partial update — only provided fields change)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Convert AvatarConfig model to dict if present
    if "avatar_config" in updates and updates["avatar_config"] is not None:
        if hasattr(updates["avatar_config"], "model_dump"):
            updates["avatar_config"] = updates["avatar_config"].model_dump()

    persona = await store.update_persona(persona_id, updates)
    if not persona:
        raise HTTPException(status_code=404, detail=f"Persona '{persona_id}' not found")
    return persona


@router.delete("/{persona_id}")
async def delete_persona(persona_id: str):
    """Delete an agent persona."""
    deleted = await store.delete_persona(persona_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Persona '{persona_id}' not found")
    return {"status": "deleted", "id": persona_id}
