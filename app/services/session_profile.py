"""
Local Session Profile Memory.

Manages persistent profile and session state via a local JSON file on disk.
This functions as the agent's "long term memory" for preferences and
remembering the context of the last cycle.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# In the container /app/data is a root-owned mount parent (compose mounts
# ./data/charts) while the app runs as appusr — writes there raise EACCES on
# every cycle ("Lesson store write failed: Permission denied"). Prefer an env
# override, fall back to ./data, and degrade to the tmp dir when unwritable.
PROFILE_FILE = Path(os.environ.get("PROFILE_DATA_DIR", "data")) / "session_profile.json"
_fallback_warned = False


def _writable_profile_path() -> "Path":
    """Return PROFILE_FILE if its directory is writable, else a tmp-dir fallback."""
    global _fallback_warned
    parent = PROFILE_FILE.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if os.access(parent, os.W_OK):
            return PROFILE_FILE
    except OSError:
        pass
    fallback = Path(tempfile.gettempdir()) / "session_profile.json"
    if not _fallback_warned:
        _fallback_warned = True
        logger.warning(
            "[SessionProfile] %s not writable — using %s (profile memory won't survive restarts)",
            parent, fallback,
        )
    return fallback


class LocalProfileMemory:
    """Manages the persistent disk-based JSON memory for the agent."""

    @staticmethod
    def _ensure_file():
        path = _writable_profile_path()
        if not path.exists():
            default_state = {
                "user_preferences": {},
                "last_trade_context": {},
                "agent_notes": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default_state, f, indent=4)
        return path

    @classmethod
    def get_profile(cls) -> dict:
        """Read the entire profile from disk."""
        try:
            path = cls._ensure_file()
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to read profile memory: %s", e)
            return {}

    @classmethod
    def save_profile(cls, data: dict):
        """Save the entire profile to disk."""
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            path = cls._ensure_file()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error("Failed to save profile memory: %s", e)

    @classmethod
    def update_preferences(cls, key: str, value: any):
        """Update a specific user preference."""
        profile = cls.get_profile()
        prefs = profile.get("user_preferences", {})
        prefs[key] = value
        profile["user_preferences"] = prefs
        cls.save_profile(profile)

    @classmethod
    def add_agent_note(cls, note: str):
        """Add a general note or memory for the agent."""
        profile = cls.get_profile()
        notes = profile.get("agent_notes", [])
        notes.append(
            {"timestamp": datetime.now(timezone.utc).isoformat(), "note": note}
        )
        profile["agent_notes"] = notes
        cls.save_profile(profile)

    @classmethod
    def set_last_trade_context(cls, context: dict):
        """Save the context of the last completed trading cycle."""
        profile = cls.get_profile()
        profile["last_trade_context"] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": context,
        }
        cls.save_profile(profile)

    @classmethod
    def get_last_trade_context(cls) -> dict:
        """Retrieve the last trading cycle context."""
        profile = cls.get_profile()
        return profile.get("last_trade_context", {})


profile_memory = LocalProfileMemory()
