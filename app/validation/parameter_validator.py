"""
Parameter Validator — pre-write checks for agent-proposed parameter changes.

Mirrors ScheduleValidator's contract: validate_proposal(...) returns
(ok, why, normalized) where `why` on rejection is a teach-y message the agent
can learn from (it states the bound/cooldown that was violated).

Safety asymmetry:
  * TIGHTENING (safer) — applies immediately, no TTL, cooldown waived.
  * LOOSENING (riskier) — mandatory TTL (auto-revert), per-key cooldown,
    and the global daily change budget.
  * NEUTRAL — cooldown + daily budget apply; TTL optional.

The bounds/tier/cooldown values live in the code-owned PARAMETER_REGISTRY
(app/services/parameter_store.py) and are never agent-editable.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from app.db.connection import get_db
from app.services.parameter_store import (
    PARAMETER_REGISTRY,
    RISK_DOWN,
    RISK_UP,
    TIER_BOARD,
    get_param,
)

logger = logging.getLogger(__name__)

# Agents allowed to propose changes at all (analysts get read-only).
STANDARD_TIER_AGENTS = {
    "v3_portfolio_manager",
    "v3_board_of_directors",
    "v3_decision_synthesizer",
    "user_chat",  # human-driven chat proxy
}
BOARD_TIER_AGENTS = {
    "v3_board_of_directors",
    "user_chat",
}

MAX_CHANGES_PER_DAY = 8      # global budget across all keys (bot-initiated)
MIN_REASON_LEN = 10


@dataclass
class NormalizedChange:
    key: str
    value: float
    is_loosening: bool
    ttl_hours: float | None   # None = permanent (tightening/neutral only)


def _classify(key: str, new_value: float) -> bool:
    """True when the proposed value LOOSENS risk vs the current effective value."""
    spec = PARAMETER_REGISTRY[key]
    current = float(get_param(key))
    if spec.direction == RISK_UP:
        return new_value > current
    if spec.direction == RISK_DOWN:
        return new_value < current
    return False  # neutral: never classified as loosening


def _last_change_age_hours(key: str) -> float | None:
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) / 3600.0 "
                "FROM runtime_parameters WHERE param_key = %s",
                [key],
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[param-validator] %s: cooldown lookup failed: %s", key, e)
        return None  # fail open on the cooldown only — bounds still enforced


def _changes_last_24h() -> int:
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM runtime_parameters "
                "WHERE created_at >= NOW() - INTERVAL '24 hours'",
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        logger.warning("[param-validator] daily-budget lookup failed: %s", e)
        return 0


class ParameterValidator:
    @staticmethod
    def validate_proposal(
        key: str,
        value,
        agent: str,
        reason: str,
        ttl_hours=None,
    ) -> tuple[bool, str, NormalizedChange | None]:
        spec = PARAMETER_REGISTRY.get(key)
        if spec is None:
            known = ", ".join(sorted(PARAMETER_REGISTRY))
            return False, f"Unknown parameter '{key}'. Tunable parameters: {known}.", None

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, f"'{key}' needs a numeric value, got {value!r}.", None
        value = float(value)
        if not math.isfinite(value):
            return False, f"'{key}' needs a FINITE numeric value, got {value!r}.", None

        if not (spec.min_value <= value <= spec.max_value):
            return False, (
                f"{key}={value} is outside the safety envelope "
                f"[{spec.min_value}, {spec.max_value}]. The bounds are code-owned "
                f"and cannot be changed by agents — propose a value inside them."
            ), None

        if len(str(reason or "").strip()) < MIN_REASON_LEN:
            return False, (
                "A specific reason is required (what changed in the market or the "
                "book that justifies this new value)."
            ), None

        allowed = BOARD_TIER_AGENTS if spec.tier == TIER_BOARD else STANDARD_TIER_AGENTS
        if agent not in allowed:
            return False, (
                f"'{agent}' is not authorized to change {key} "
                f"(tier '{spec.tier}' — allowed: {', '.join(sorted(allowed))})."
            ), None

        if ttl_hours is not None:
            if (
                isinstance(ttl_hours, bool)
                or not isinstance(ttl_hours, (int, float))
                or not math.isfinite(float(ttl_hours))
                or ttl_hours <= 0
            ):
                # NaN evades plain <=0 / >max comparisons (both are False for
                # NaN) — an adversarial NaN TTL reached the DB write intact.
                return False, "ttl_hours must be a positive FINITE number of hours.", None
            if ttl_hours > spec.max_ttl_hours:
                return False, (
                    f"ttl_hours={ttl_hours} exceeds the max for {key} "
                    f"({spec.max_ttl_hours}h). Re-affirm the change later instead."
                ), None

        is_loosening = _classify(key, value)
        effective_ttl = ttl_hours
        if is_loosening:
            # Risk-loosening changes always expire → auto-revert unless re-affirmed.
            if effective_ttl is None:
                effective_ttl = spec.loosen_ttl_hours
            age = _last_change_age_hours(key)
            if age is not None and age < spec.cooldown_hours:
                return False, (
                    f"{key} was changed {age:.1f}h ago; risk-loosening changes have a "
                    f"{spec.cooldown_hours:.0f}h cooldown. Work with the current value "
                    f"({get_param(key)}) or wait."
                ), None
            daily = _changes_last_24h()
            if daily >= MAX_CHANGES_PER_DAY:
                return False, (
                    f"Daily parameter-change budget spent ({daily}/{MAX_CHANGES_PER_DAY} "
                    "in 24h). Only the most important adjustment gets made — try tomorrow."
                ), None

        return True, "", NormalizedChange(
            key=key, value=value, is_loosening=is_loosening,
            ttl_hours=float(effective_ttl) if effective_ttl is not None else None,
        )
