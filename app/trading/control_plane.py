"""Control Plane Rollout Modes and Precedence Router.

Defines the three explicit operational modes:
- OBSERVE: Existing paper execution is authoritative; new policy results are advisory.
           Unattributed legacy executions are recorded as LEGACY_MIGRATION_BYPASS
           without distorting executor defect statistics.
- SHADOW:  Proposals and execution are simulated without mutating the paper portfolio.
           Results are persisted to shadow collections.
- ENFORCE: Approved execution intent is strictly required for paper entry.
           Rejection or missing intent aborts the trade before reaching the executor.
           FAIL_CLOSED is the failure behavior of ENFORCE, not a separate rollout mode.
"""

from __future__ import annotations

import enum
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ControlPlaneMode(str, enum.Enum):
    OBSERVE = "OBSERVE"
    SHADOW = "SHADOW"
    ENFORCE = "ENFORCE"


def resolve_control_plane_mode(bot_id: Optional[str] = None) -> ControlPlaneMode:
    """Resolve effective control plane mode from environment and optional account overrides."""
    # 1. Check account-specific override from environment: CONTROL_PLANE_MODE_<BOT_ID>
    if bot_id:
        env_bot_mode = os.getenv(f"CONTROL_PLANE_MODE_{bot_id.upper().replace('-', '_')}")
        if env_bot_mode and env_bot_mode.upper() in ControlPlaneMode.__members__:
            return ControlPlaneMode(env_bot_mode.upper())

    # 2. Check global env var first, then settings
    raw_mode = os.getenv("CONTROL_PLANE_MODE")
    if not raw_mode:
        from app.config import settings
        raw_mode = getattr(settings, "CONTROL_PLANE_MODE", "OBSERVE")
    raw_mode = str(raw_mode).strip().upper()

    if raw_mode in ControlPlaneMode.__members__:
        return ControlPlaneMode(raw_mode)

    logger.warning(
        "[ControlPlane] Unrecognized mode %r — falling back to fail-safe OBSERVE",
        raw_mode,
    )
    return ControlPlaneMode.OBSERVE


def is_enforce_active(mode: ControlPlaneMode) -> bool:
    """Returns True if intent enforcement is mandatory."""
    return mode == ControlPlaneMode.ENFORCE


def is_shadow_active(mode: ControlPlaneMode) -> bool:
    """Returns True if execution should simulate without mutating paper portfolio."""
    return mode == ControlPlaneMode.SHADOW
