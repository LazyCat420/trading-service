"""
agent_persona_store.py — Flat JSON file CRUD for agent persona profiles.

Storage approach: single JSON file at app/config/agent_personas.json
- Zero-migration (no DB schema changes needed)
- Auto-seeds from hardcoded PERSONAS dict on first load
- Thread-safe with asyncio.Lock
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Storage path — sits next to personas.py in the config directory
_STORE_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
_STORE_PATH = os.path.join(_STORE_DIR, "agent_personas.json")
_STORE_PATH = os.path.normpath(_STORE_PATH)

_lock = asyncio.Lock()
_cache: dict | None = None


def _default_avatar_for_role(role: str) -> dict:
    """Return role-appropriate default avatar colors."""
    defaults = {
        "QUANT": {
            "skin_color": "#f5deb3",
            "hair_color": "#4a4a4a",
            "outfit_color": "#1e3a5f",
            "accent_color": "#38bdf8",
            "accessory": "glasses",
        },
        "TECHNICAL": {
            "skin_color": "#f5deb3",
            "hair_color": "#2d3748",
            "outfit_color": "#0891b2",
            "accent_color": "#06b6d4",
            "accessory": "glasses",
        },
        "FUNDAMENTAL": {
            "skin_color": "#c68642",
            "hair_color": "#1a1a2e",
            "outfit_color": "#7c3aed",
            "accent_color": "#f59e0b",
            "accessory": "tie",
        },
        "BEHAVIORAL": {
            "skin_color": "#f5deb3",
            "hair_color": "#8b4513",
            "outfit_color": "#dc2626",
            "accent_color": "#fbbf24",
            "accessory": None,
        },
        "RISK": {
            "skin_color": "#ffe0bd",
            "hair_color": "#d4a574",
            "outfit_color": "#374151",
            "accent_color": "#ef4444",
            "accessory": "glasses",
        },
        "DATA_JANITOR": {
            "skin_color": "#f5deb3",
            "hair_color": "#6b7280",
            "outfit_color": "#78716c",
            "accent_color": "#a3e635",
            "accessory": None,
        },
        "PM": {
            "skin_color": "#f5deb3",
            "hair_color": "#1f2937",
            "outfit_color": "#0f172a",
            "accent_color": "#d4af37",
            "accessory": "tie",
        },
    }
    return defaults.get(role, {
        "skin_color": "#fde68a",
        "hair_color": "#1e293b",
        "outfit_color": "#3b82f6",
        "accent_color": "#f59e0b",
        "accessory": None,
    })


def _seed_from_hardcoded() -> dict:
    """Create initial personas from the hardcoded PERSONAS dict."""
    from app.config.personas import PERSONAS

    now = datetime.now(timezone.utc).isoformat()
    store = {}

    # Deterministic IDs so re-seeding doesn't create duplicates
    role_order = {
        "DATA_JANITOR": 1,
        "QUANT": 2,
        "FUNDAMENTAL": 3,
        "BEHAVIORAL": 4,
        "RISK": 5,
        "PM": 6,
    }

    for role_key, persona in PERSONAS.items():
        persona_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agent-studio.{role_key}"))
        store[persona_id] = {
            "id": persona_id,
            "name": persona["name"],
            "display_name": persona["name"],
            "role": role_key,
            "system_prompt": persona["prompt"],
            "voice_pitch": 1.0,
            "voice_rate": 1.15,
            "avatar_config": _default_avatar_for_role(role_key),
            "allowed_tools": [],
            "execution_order": role_order.get(role_key, 5),
            "is_active": True,
            "max_tokens": 2048,
            "temperature": 0.7,
            "created_at": now,
            "updated_at": now,
        }

    logger.info("[AgentPersonaStore] Seeded %d personas from hardcoded PERSONAS", len(store))
    return store


def _load_store() -> dict:
    """Load the JSON store from disk, seeding if it doesn't exist."""
    global _cache
    if _cache is not None:
        return _cache

    if os.path.exists(_STORE_PATH):
        try:
            with open(_STORE_PATH, "r") as f:
                _cache = json.load(f)
            logger.info("[AgentPersonaStore] Loaded %d personas from %s", len(_cache), _STORE_PATH)
            return _cache
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[AgentPersonaStore] Failed to load store, re-seeding: %s", e)

    _cache = _seed_from_hardcoded()
    _save_store(_cache)
    return _cache


def _save_store(data: dict) -> None:
    """Write the store to disk."""
    global _cache
    _cache = data
    os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
    # Write to tmp file then rename for atomic writes
    tmp_path = _STORE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, _STORE_PATH)


# ── Public CRUD API ──────────────────────────────────────────────────────────


async def list_personas() -> list[dict]:
    """Return all personas sorted by execution_order."""
    async with _lock:
        store = _load_store()
        personas = list(store.values())
        personas.sort(key=lambda p: p.get("execution_order", 99))
        return personas


async def get_persona(persona_id: str) -> Optional[dict]:
    """Return a single persona by ID."""
    async with _lock:
        store = _load_store()
        return store.get(persona_id)


async def get_persona_by_role(role: str) -> Optional[dict]:
    """Return the first active persona matching a role key."""
    async with _lock:
        store = _load_store()
        for p in store.values():
            if p.get("role") == role and p.get("is_active", True):
                return p
        return None


async def create_persona(data: dict) -> dict:
    """Create a new persona. Returns the created record."""
    async with _lock:
        store = _load_store()
        persona_id = data.get("id") or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        record = {
            "id": persona_id,
            "name": data["name"],
            "display_name": data.get("display_name", data["name"]),
            "role": data["role"],
            "system_prompt": data["system_prompt"],
            "voice_pitch": data.get("voice_pitch", 1.0),
            "voice_rate": data.get("voice_rate", 1.15),
            "avatar_config": data.get("avatar_config") or _default_avatar_for_role(data["role"]),
            "allowed_tools": data.get("allowed_tools", []),
            "execution_order": data.get("execution_order", len(store) + 1),
            "is_active": data.get("is_active", True),
            "max_tokens": data.get("max_tokens", 2048),
            "temperature": data.get("temperature", 0.7),
            "created_at": now,
            "updated_at": now,
        }

        store[persona_id] = record
        _save_store(store)
        logger.info("[AgentPersonaStore] Created persona '%s' (%s)", record["name"], persona_id)
        return record


async def update_persona(persona_id: str, updates: dict) -> Optional[dict]:
    """Update an existing persona. Returns the updated record or None."""
    async with _lock:
        store = _load_store()
        if persona_id not in store:
            return None

        record = store[persona_id]
        # Only update fields that are explicitly provided (not None)
        for key, value in updates.items():
            if value is not None and key not in ("id", "created_at"):
                record[key] = value

        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        store[persona_id] = record
        _save_store(store)
        logger.info("[AgentPersonaStore] Updated persona '%s' (%s)", record["name"], persona_id)
        return record


async def delete_persona(persona_id: str) -> bool:
    """Delete a persona. Returns True if deleted, False if not found."""
    async with _lock:
        store = _load_store()
        if persona_id not in store:
            return False

        deleted = store.pop(persona_id)
        _save_store(store)
        logger.info("[AgentPersonaStore] Deleted persona '%s' (%s)", deleted.get("name"), persona_id)
        return True


async def reset_to_defaults() -> list[dict]:
    """Re-seed from hardcoded PERSONAS, replacing all current data."""
    async with _lock:
        global _cache
        store = _seed_from_hardcoded()
        _save_store(store)
        personas = list(store.values())
        personas.sort(key=lambda p: p.get("execution_order", 99))
        return personas
