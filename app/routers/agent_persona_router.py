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
