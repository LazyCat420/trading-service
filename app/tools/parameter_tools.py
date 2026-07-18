"""
Parameter tools — let agents see and (governed) adjust runtime trading
parameters instead of every threshold being hardcoded.

All writes go through the Parameter Governor (app/services/parameter_governor),
which enforces the code-owned safety envelope: hard min/max bounds per
parameter, authorization tiers, cooldowns, a daily change budget, and
mandatory auto-revert TTLs on risk-loosening changes.
"""

import json
import logging

from app.tools.registry import registry, PermissionLevel
from app.tools.tool_context import current_agent_name

logger = logging.getLogger(__name__)


@registry.register(
    name="get_parameters",
    description=(
        "List every runtime-tunable trading parameter: current effective value, default, "
        "hard min/max bounds, authorization tier, and the most recent change (who/why/expiry). "
        "Covers sizing caps, confidence/data-quality floors, drawdown breaker, ATR stop multiplier, "
        "take-profit R:R, triage thresholds, research/watch budgets, and cadences. "
        "Check this BEFORE reasoning about risk limits — the live values may differ from your priors."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    tier=1,
    source="parameter_governance",
    permission=PermissionLevel.READ_ONLY,
)
async def get_parameters() -> str:
    from app.services.parameter_governor import list_parameters
    try:
        return json.dumps(list_parameters(), default=str)
    except Exception as e:
        logger.error("[ParameterTools] get_parameters failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="propose_parameter_change",
    description=(
        "Propose a new value for a runtime trading parameter (see get_parameters for keys and "
        "bounds). The governor enforces hard code-owned bounds, per-key cooldowns, a daily change "
        "budget, and authorization tiers (some parameters are board-only). Risk-TIGHTENING changes "
        "apply immediately and persist; risk-LOOSENING changes require justification and auto-revert "
        "after a TTL unless re-affirmed. Give a specific, evidence-based reason — vague reasons are rejected."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Parameter key exactly as listed by get_parameters (e.g. MAX_POSITION_SIZE_PCT).",
            },
            "value": {
                "type": "number",
                "description": "Proposed new value — must lie inside the parameter's hard bounds.",
            },
            "reason": {
                "type": "string",
                "description": "Specific market/book evidence justifying the change.",
            },
            "ttl_hours": {
                "type": "number",
                "description": "Optional: hours until the change auto-reverts. Loosening changes get a default TTL if omitted.",
            },
        },
        "required": ["key", "value", "reason"],
    },
    tier=1,
    source="parameter_governance",
    permission=PermissionLevel.WRITE,
)
async def propose_parameter_change(key: str, value: float, reason: str, ttl_hours: float = None) -> str:
    from app.services.parameter_governor import propose_parameter_change as _go
    agent = current_agent_name()
    logger.info("[ParameterTools] propose_parameter_change by %s: %s=%s (%s)", agent, key, value, reason)
    try:
        return json.dumps(_go(key, value, reason, ttl_hours=ttl_hours), default=str)
    except Exception as e:
        logger.error("[ParameterTools] propose_parameter_change failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
