"""
Post-parse validators for V3 agent artifacts.

The 2026-07-21 research audit found schema drift that nothing caught:
trade_results rows with regime = the literal enum string
"HIGH_VOLATILITY|DEEP_DISCOUNT|CONTRADICTORY", factors outside [0,1], and
HOLD decisions whose dynamic_trigger carried value=null — which
order_triggers.check_price_triggers() gates on (`dynamic_trigger_value is
not None`), so those watches could NEVER fire.

Validators coerce in place and never raise: a malformed field degrades to a
safe value + a note in `_validator_notes`, not a failed run.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

VALID_REGIMES = ("HIGH_VOLATILITY", "DEEP_DISCOUNT", "CONTRADICTORY")

# Evaluation semantics per dynamic trigger family (order_triggers.py):
#   sma_*/rsi_* — compares live price vs the live metric; the stored value is
#     unused BUT `is not None` gates the whole branch → 0.0 placeholder is safe
#     and makes the watch actually evaluable.
#   trailing_drop — value IS the trail fraction; a sane default beats a dead row.
_TRAILING_DEFAULT = 0.10


def _note(artifact: dict, msg: str) -> None:
    artifact.setdefault("_validator_notes", []).append(msg)


def validate_regime_artifact(artifact: dict) -> dict:
    """Coerce the regime enum, clamp factors to [0,1], normalize mods list."""
    if not isinstance(artifact, dict):
        return artifact

    regime = str(artifact.get("regime", "")).strip().upper()
    if regime not in VALID_REGIMES:
        # Models occasionally echo the schema literal ("A|B|C") or invent a
        # label. CONTRADICTORY is the codebase-wide safe fallback persona.
        coerced = next((r for r in VALID_REGIMES if regime and regime in r), None)
        fixed = coerced or "CONTRADICTORY"
        _note(artifact, f"regime '{artifact.get('regime')}' coerced to {fixed}")
        logger.warning("[ArtifactValidator] invalid regime %r → %s", artifact.get("regime"), fixed)
        artifact["regime"] = fixed

    factors = artifact.get("factors")
    if isinstance(factors, dict):
        for key, val in list(factors.items()):
            try:
                f = float(val)
            except (TypeError, ValueError):
                _note(artifact, f"factor {key}={val!r} not numeric — dropped")
                factors.pop(key)
                continue
            clamped = min(1.0, max(0.0, f))
            if clamped != f:
                _note(artifact, f"factor {key}={f} clamped to {clamped}")
            factors[key] = clamped

    mods = artifact.get("suggested_pipeline_modifications")
    if mods is None:
        artifact["suggested_pipeline_modifications"] = []
    elif isinstance(mods, str):
        artifact["suggested_pipeline_modifications"] = [mods] if mods.strip() else []
    elif isinstance(mods, list):
        artifact["suggested_pipeline_modifications"] = [str(m) for m in mods if m]

    return artifact


def validate_trade_decision_artifact(artifact: dict) -> dict:
    """Make dynamic_trigger actually evaluable (value=None never fires)."""
    if not isinstance(artifact, dict):
        return artifact

    trigger = artifact.get("dynamic_trigger")
    if not isinstance(trigger, dict):
        return artifact

    t_type = str(trigger.get("type") or "").strip()
    if not t_type or t_type.lower() in ("none", "null"):
        artifact["dynamic_trigger"] = None
        return artifact

    value = trigger.get("value")
    if value is not None:
        try:
            trigger["value"] = float(value)
            return artifact
        except (TypeError, ValueError):
            _note(artifact, f"dynamic_trigger.value {value!r} not numeric — refilled")
            value = None

    if value is None:
        if t_type == "trailing_drop":
            trigger["value"] = _TRAILING_DEFAULT
            _note(artifact, f"dynamic_trigger.value missing — defaulted trailing_drop to {_TRAILING_DEFAULT}")
        elif t_type.startswith("rsi_"):
            # RSI triggers ARE threshold crossings of the oscillator itself —
            # default to the conventional levels instead of a 0.0 placeholder
            # (order_triggers also guards, but the stored row should be honest).
            trigger["value"] = 30.0 if "oversold" in t_type else 70.0
            _note(artifact, f"dynamic_trigger.value missing — defaulted {t_type} to {trigger['value']}")
        elif t_type.startswith("sma_"):
            # Evaluation compares price vs the live metric; value only needs
            # to be non-null for the branch to run at all.
            trigger["value"] = 0.0
            _note(artifact, "dynamic_trigger.value missing — set 0.0 placeholder (metric-relative trigger)")
        else:
            # Unknown type with no threshold can never evaluate — drop it so
            # a dead watch row is never registered.
            _note(artifact, f"dynamic_trigger type '{t_type}' had no value — trigger dropped")
            artifact["dynamic_trigger"] = None
        logger.info("[ArtifactValidator] dynamic_trigger normalized: %s", artifact.get("dynamic_trigger"))

    return artifact


_VALIDATORS = {
    "regime_classification": validate_regime_artifact,
    "trade_decision": validate_trade_decision_artifact,
    # The board's final_decision carries the same dynamic_trigger shape.
    "final_decision": validate_trade_decision_artifact,
}


def validate_artifact(artifact_type: str, artifact: dict) -> dict:
    """Dispatch to the per-type validator; identity for unknown types."""
    validator = _VALIDATORS.get(artifact_type)
    if not validator:
        return artifact
    try:
        return validator(artifact)
    except Exception as e:
        logger.warning("[ArtifactValidator] %s validation failed (artifact kept as-is): %s", artifact_type, e)
        return artifact
