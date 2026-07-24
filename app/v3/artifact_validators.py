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

    # forward_call is the engine's one gradeable claim (scored 5 trading days
    # later against SPX/VIX). The grader matches on exact enums, so normalize
    # case and drop the schema literal models sometimes echo back verbatim —
    # an un-normalized "Up" would silently score as a miss forever.
    fc = artifact.get("forward_call")
    if isinstance(fc, dict):
        for key, valid in (
            ("spx_direction", {"UP", "DOWN", "FLAT"}),
            ("vol_direction", {"RISING", "FALLING", "STABLE"}),
        ):
            raw = str(fc.get(key, "")).strip().upper()
            if raw not in valid:
                if raw:
                    _note(artifact, f"forward_call.{key}={fc.get(key)!r} invalid — dropped")
                fc.pop(key, None)
            else:
                fc[key] = raw
        try:
            conviction = float(fc.get("conviction"))
            fc["conviction"] = min(100.0, max(0.0, conviction))
        except (TypeError, ValueError):
            fc.pop("conviction", None)
        if not fc.get("spx_direction") and not fc.get("vol_direction"):
            artifact.pop("forward_call", None)
    elif fc is not None:
        artifact.pop("forward_call", None)

    return artifact


def validate_desk_note_artifact(artifact: dict) -> dict:
    """Normalize the junior's routing field and its falsifiable claim.

    triage_recommendation drives orchestrator routing and catalyst_call is
    graded against the tape — both are matched on exact enums, so a stray
    "full" or an echoed schema literal would silently route to the default or
    score as a permanent miss.
    """
    if not isinstance(artifact, dict):
        return artifact

    valid_triage = {"FULL", "QUANT_ONLY", "SKIP"}
    triage = str(artifact.get("triage_recommendation", "")).strip().upper()
    if triage and triage not in valid_triage:
        # The orchestrator already treats unrecognized values as FULL; make
        # that explicit in the stored artifact rather than implicit in routing.
        _note(artifact, f"triage_recommendation {artifact.get('triage_recommendation')!r} → FULL")
        artifact["triage_recommendation"] = "FULL"
    elif triage:
        artifact["triage_recommendation"] = triage

    call = artifact.get("catalyst_call")
    if isinstance(call, dict):
        direction = str(call.get("direction", "")).strip().upper()
        if direction in {"BULLISH", "BEARISH", "NEUTRAL"}:
            call["direction"] = direction
        else:
            if direction:
                _note(artifact, f"catalyst_call.direction={call.get('direction')!r} invalid — dropped")
            call.pop("direction", None)
        try:
            call["conviction"] = min(100.0, max(0.0, float(call.get("conviction"))))
        except (TypeError, ValueError):
            call.pop("conviction", None)
        priced = call.get("already_priced_in")
        if isinstance(priced, str):
            lowered = priced.strip().lower()
            if lowered in ("true", "yes"):
                call["already_priced_in"] = True
            elif lowered in ("false", "no"):
                call["already_priced_in"] = False
            else:
                call.pop("already_priced_in", None)
        if not call.get("direction"):
            artifact.pop("catalyst_call", None)
    elif call is not None:
        artifact.pop("catalyst_call", None)

    return artifact


def validate_fundamental_report_artifact(artifact: dict) -> dict:
    """Normalize the horizon fields added by the 2026-07-24 audit.

    near_term_read.direction is what downstream desks weigh for a trade that
    resolves in ~7 days, and what the scorecard grades, so a stray "bullish"
    or an echoed schema literal must not survive as an unmatched string.
    """
    if not isinstance(artifact, dict):
        return artifact

    # thesis_direction is REQUIRED and consumed directionally by the
    # contradiction shadow, the debate and the scorecard — but 5 live artifacts
    # emitted the schema placeholder "BULLISH|BEARISH|NEUTRAL" verbatim, which
    # every consumer then read as an unmatched string. Coerce to the
    # least-committal stance and say so, matching the regime artifact's
    # fallback convention.
    thesis = str(artifact.get("thesis_direction", "")).strip().upper()
    if thesis and thesis not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        _note(artifact, f"thesis_direction {artifact.get('thesis_direction')!r} → NEUTRAL")
        artifact["thesis_direction"] = "NEUTRAL"
    elif thesis:
        artifact["thesis_direction"] = thesis

    horizon = str(artifact.get("horizon", "")).strip().upper()
    if horizon:
        if horizon in {"WEEKS", "QUARTERS", "YEARS"}:
            artifact["horizon"] = horizon
        else:
            _note(artifact, f"horizon {artifact.get('horizon')!r} invalid — dropped")
            artifact.pop("horizon", None)

    read = artifact.get("near_term_read")
    if isinstance(read, dict):
        direction = str(read.get("direction", "")).strip().upper()
        if direction in {"BULLISH", "BEARISH", "NEUTRAL"}:
            read["direction"] = direction
        else:
            if direction:
                _note(artifact, f"near_term_read.direction={read.get('direction')!r} invalid — dropped")
            read.pop("direction", None)

        matters = read.get("matters_this_week")
        if isinstance(matters, str):
            lowered = matters.strip().lower()
            if lowered in ("true", "yes"):
                read["matters_this_week"] = True
            elif lowered in ("false", "no"):
                read["matters_this_week"] = False
            else:
                read.pop("matters_this_week", None)
        elif matters is not None and not isinstance(matters, bool):
            read.pop("matters_this_week", None)

        # A read with nothing directional in it tells downstream nothing.
        if not read.get("direction"):
            artifact.pop("near_term_read", None)
    elif read is not None:
        artifact.pop("near_term_read", None)

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
    "desk_note": validate_desk_note_artifact,
    "fundamental_report": validate_fundamental_report_artifact,
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
