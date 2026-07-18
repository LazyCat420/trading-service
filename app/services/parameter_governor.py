"""
Parameter Governor — the ONLY writer of runtime_parameters.

Modeled on the Research Governor pattern: agents propose, the governor
validates against the code-owned safety envelope (ParameterValidator),
writes an auditable row, and returns a structured result — including
teach-y rejections so the agent learns the bounds instead of flailing.

Change semantics:
  * Tightening (safer) — permanent (no expiry) until changed again.
  * Loosening (riskier) — stamped with a TTL; when it lapses, resolution
    falls through to the previous still-active row or the default
    (automatic revert, no background job needed).

Cadence parameters additionally get a best-effort live resync of their
APScheduler job (cycle_scheduler picks up the rest on its periodic sync).
"""

from __future__ import annotations

import logging

from app.db.connection import get_db
from app.services.parameter_store import (
    PARAMETER_REGISTRY,
    get_param,
    get_param_record,
    invalidate_cache,
)
from app.validation.parameter_validator import ParameterValidator

logger = logging.getLogger(__name__)


def list_parameters() -> dict:
    """Every tunable parameter: current value, envelope, and last change."""
    return {
        "status": "ok",
        "parameters": [get_param_record(key) for key in sorted(PARAMETER_REGISTRY)],
        "note": (
            "Values apply system-wide. Bounds/tier/cooldowns are code-owned. "
            "Risk-loosening changes expire automatically (TTL) unless re-affirmed."
        ),
    }


def propose_parameter_change(
    key: str,
    value,
    reason: str,
    ttl_hours=None,
    agent: str | None = None,
) -> dict:
    """Validate and apply an agent-proposed parameter change."""
    if agent is None:
        try:
            from app.tools.tool_context import current_agent_name
            agent = current_agent_name() or "unknown"
        except Exception:  # noqa: BLE001
            agent = "unknown"

    key = str(key or "").strip()
    ok, why, change = ParameterValidator.validate_proposal(
        key, value, agent=agent, reason=reason, ttl_hours=ttl_hours
    )
    if not ok:
        logger.info("[PARAM-GOV] REJECTED %s=%s by %s: %s", key, value, agent, why)
        return {"status": "rejected", "reason": why}

    old_value = get_param(key)
    try:
        with get_db() as db:
            if change.ttl_hours is not None:
                db.execute(
                    """
                    INSERT INTO runtime_parameters
                        (param_key, value, set_by, reason, status, expires_at)
                    VALUES (%s, %s, %s, %s, 'active',
                            NOW() + make_interval(hours => %s))
                    """,
                    [change.key, change.value, agent, reason, change.ttl_hours],
                )
            else:
                db.execute(
                    """
                    INSERT INTO runtime_parameters
                        (param_key, value, set_by, reason, status, expires_at)
                    VALUES (%s, %s, %s, %s, 'active', NULL)
                    """,
                    [change.key, change.value, agent, reason],
                )
    except Exception as e:  # noqa: BLE001
        logger.error("[PARAM-GOV] write failed for %s: %s", key, e)
        return {"status": "error", "message": f"Store write failed: {e}"}

    invalidate_cache(key)
    new_value = get_param(key)
    logger.warning(
        "[PARAM-GOV] %s: %s -> %s by %s (%s%s) — %s",
        key, old_value, new_value, agent,
        "LOOSENING" if change.is_loosening else "tightening/neutral",
        f", auto-reverts in {change.ttl_hours:.0f}h" if change.ttl_hours else "",
        reason[:200],
    )

    _maybe_sync_scheduler(key)

    return {
        "status": "applied",
        "key": key,
        "previous_value": old_value,
        "new_value": new_value,
        "loosening": change.is_loosening,
        "expires_in_hours": change.ttl_hours,
        "note": (
            "Risk-loosening change — auto-reverts at expiry unless re-affirmed."
            if change.is_loosening else "Applied until changed again."
        ),
    }


def _maybe_sync_scheduler(key: str) -> None:
    """Best-effort live retune of the APScheduler job behind a cadence param.

    Failure is fine: cycle_scheduler's periodic cadence-sync job reconciles
    intervals from the store anyway (and tool calls may run in a process
    without the scheduler engine).
    """
    spec = PARAMETER_REGISTRY[key]
    if not spec.scheduler_job:
        return
    try:
        from app.services.cycle_scheduler import SchedulerService
        SchedulerService.sync_cadence_jobs()
    except Exception as e:  # noqa: BLE001
        logger.info("[PARAM-GOV] scheduler sync deferred for %s: %s", key, e)
