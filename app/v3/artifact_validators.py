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


# Agents speak different dialects: analysts emit thesis_direction, decision
# makers emit action, the debate emits winning_side. Kept in sync with
# scripts/agent_scorecard.py::_DIRECTION_MAP so the shadow flag and the
# scorecard classify the same desk identically.
_STANCE_MAP = {
    "BULLISH": 1, "BUY": 1, "BULL": 1, "LONG": 1,
    "BEARISH": -1, "SELL": -1, "BEAR": -1, "SHORT": -1,
    "NEUTRAL": 0, "HOLD": 0, "TIE": 0, "SPLIT": 0,
}


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
        # Open item 2 (2026-08-05): a coerced label must stay distinguishable
        # from a genuine classification — CONTRADICTORY-by-fallback and
        # CONTRADICTORY-by-judgement previously produced identical artifacts.
        artifact["regime_fallback"] = True

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


_VALUATION_VERDICTS = {"OVERVALUED", "FAIR", "UNDERVALUED", "NOT_ASSESSABLE"}


def validate_valuation_report_artifact(artifact: dict) -> dict:
    """Normalize the valuation verdict and drop non-numeric metrics.

    Two failures this closes, both learned next door on fundamental_report:

    1. Live artifacts emit the schema placeholder verbatim — 5 fundamental
       reports shipped `"BULLISH|BEARISH|NEUTRAL"` as a literal, which every
       consumer then read as an unmatched string. `verdict` gets the same
       treatment, but coerces to NOT_ASSESSABLE rather than FAIR: an
       unparseable verdict is an ABSENT judgement, and calling it FAIR would
       manufacture a neutral opinion the agent never expressed.

    2. A non-numeric metric must be DROPPED, never zeroed. A zero EV/EBIT
       reads downstream as an extraordinarily cheap company; an absent one
       reads as absent. The reconcile pass fills whatever it can verify
       afterwards, so dropping loses nothing that was real.
    """
    if not isinstance(artifact, dict):
        return artifact

    verdict = str(artifact.get("verdict", "")).strip().upper()
    if verdict and verdict not in _VALUATION_VERDICTS:
        _note(artifact, f"verdict {artifact.get('verdict')!r} → NOT_ASSESSABLE")
        artifact["verdict"] = "NOT_ASSESSABLE"
    elif verdict:
        artifact["verdict"] = verdict

    metrics = artifact.get("valuation_metrics")
    if isinstance(metrics, dict):
        for key in list(metrics):
            val = metrics[key]
            if isinstance(val, bool):
                dropped = True
            else:
                try:
                    num = float(val)
                    # NaN and inf survive float() and compare false against
                    # every threshold — they must not reach the Board.
                    dropped = num != num or num in (float("inf"), float("-inf"))
                except (TypeError, ValueError):
                    dropped = True
            if dropped:
                _note(artifact, f"valuation_metrics.{key}={val!r} not numeric — dropped")
                metrics.pop(key, None)
        if not metrics:
            # An empty dict claims "we computed nothing", which is true, but a
            # present-and-empty key reads as a successful computation of
            # nothing. Reconcile repopulates it if there is anything to say.
            artifact.pop("valuation_metrics", None)
    elif metrics is not None:
        artifact.pop("valuation_metrics", None)

    rules = artifact.get("doctrine_rules_applied")
    if isinstance(rules, list):
        artifact["doctrine_rules_applied"] = [
            str(r).strip() for r in rules if str(r).strip()
        ]
    elif rules is not None:
        artifact.pop("doctrine_rules_applied", None)

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


def coerce_unshortable_sell(
    artifact: dict, *, held: bool, ticker: str = "", cycle_id: str = "",
) -> dict:
    """A SELL on a ticker the bot does not hold is not a decision — it is an
    illegal move, and there is no shorting.

    Measured over 5 weeks: 167 of 176 SELL decisions (95%) were on unheld
    tickers. Every one was blocked by the policy gate at the very END of the
    pipeline, after ~1,243s of agent time — the most expensive decisions in
    the system and the least actionable. They also polluted the decision
    record, which is what made the board look like it was destroying value.

    The prompt already says so; the constraint was being shed from the prompt
    (fixed in agent_runner) and models ignored it anyway. This is the backstop:
    the artifact is rewritten to HOLD, keeping the bearish view visible in the
    reasoning rather than pretending a sell order was reasonable.

    2026-07-25: this used to log a line naming neither the ticker nor the
    cycle, and recorded the firing ONLY in artifact metadata. A reviewer
    grepped the logs for the ticker, found nothing, and concluded the guardrail
    had never fired when in fact it had — the false conclusion being that the
    prompt fix alone was sufficient. A guardrail nobody can count is a
    guardrail nobody can trust, so it now names the ticker and increments a
    telemetry counter.
    """
    if not isinstance(artifact, dict) or held:
        return artifact
    if str(artifact.get("action") or "").strip().upper() != "SELL":
        return artifact

    artifact["action"] = "HOLD"
    artifact["_coerced_from"] = "SELL"
    artifact["position_size_pct"] = 0
    artifact["decision_provenance"] = "coerced_unshortable"
    _note(
        artifact,
        "SELL on an unheld ticker is not executable (no shorting) — coerced to "
        "HOLD/no-position; the bearish view is retained in the reasoning",
    )
    logger.info(
        "[ArtifactValidator][GUARDRAIL] coerce_unshortable_sell FIRED — "
        "%s/%s: unheld SELL coerced to HOLD (no shorting)",
        cycle_id or "?", ticker or "?",
    )
    # Imported lazily: this module is deliberately dependency-free so the
    # validators stay unit-testable without a DB, and telemetry imports
    # shared_desk.
    try:
        from app.v3.telemetry import record_guardrail_firing

        record_guardrail_firing(
            "coerce_unshortable_sell", ticker=ticker, cycle_id=cycle_id,
            detail={"coerced_from": "SELL", "artifact": artifact.get("_artifact_type")},
        )
    except Exception as e:  # never let telemetry break a safety rewrite
        logger.warning("[ArtifactValidator] guardrail telemetry failed: %s", e)
    return artifact


def flag_bearish_override_of_fundamental(
    artifact: dict, *, fundamental_report: dict | None,
    ticker: str = "", cycle_id: str = "",
) -> dict:
    """SHADOW ONLY — mark the one board override measured to destroy value.

    Measured 2026-07-25 over 856 desks (`scripts/override_matrix.py`,
    `scripts/override_diagnosis.py`), this is the single handoff that survives
    a 20,000-iteration permutation test:

        board AGREES with the fundamental desk : +0.06%   (n=86)
        board OVERRIDES it                     : -2.32%   (n=35)
        difference -2.38%, permutation p=0.0015   [EXECUTABLE decisions only]

    And the damage is not spread across overrides — it sits in exactly one
    quadrant, from `override_diagnosis.py` H1:

        desk NEUTRAL -> board BEARISH   n=68  mean=-2.81%   <-- this
        desk BULLISH -> board BEARISH   n=13  mean=-0.87%
        desk NEUTRAL -> board BULLISH   n=18  mean=+1.60%
        desk BEARISH -> board BULLISH   n= 4  mean=+2.50%

    Every *bullish* override is positive. The costly move is specifically the
    board turning bearish over a desk that reported no near-term view. Board
    confidence does NOT discriminate (high half -0.98%, low half -0.96%,
    p=0.97), so the board cannot self-police this with a confidence threshold.

    ## Why this only FLAGS and does not rewrite

    Two reasons, both learned the hard way on this codebase.

    1. **n=35 executable overrides.** Enough to detect a -2.38% effect, not
       enough to rewire the board. The standing rule from the 2026-07-24 audit
       is that board changes ship shadow-first and are promoted only on live
       evidence.
    2. **The raw figure was mostly unexecutable.** Of the 68 damaging desks,
       42 were policy-blocked SELLs on unheld tickers and 15 were no-op HOLDs;
       only 9 could move the book. Scoring those blocked SELLs as real losses
       is exactly what produced the retracted "decision layer destroys value"
       headline. The effect survives on executable-only rows (that is the
       p=0.0015 above), but a rewrite driven by the unfiltered number would
       have been tuned on noise.

    The flag makes the population countable in `shared_desk`, so promotion to
    an actual coercion can be decided on accumulated live data rather than on
    this one 30-day window.
    """
    if not isinstance(artifact, dict) or not isinstance(fundamental_report, dict):
        return artifact

    def _dir(a: dict) -> int | None:
        read = a.get("near_term_read")
        if isinstance(read, dict):
            key = str(read.get("direction", "")).strip().upper()
            if key in _STANCE_MAP:
                return _STANCE_MAP[key]
        for field in ("thesis_direction", "action", "winning_side"):
            raw = a.get(field)
            if raw is None:
                continue
            key = str(raw).strip().upper()
            if key in _STANCE_MAP:
                return _STANCE_MAP[key]
        return None

    desk_dir, board_dir = _dir(fundamental_report), _dir(artifact)
    # The measured-costly quadrant only: desk had NO near-term view (NEUTRAL)
    # and the board went bearish anyway.
    if desk_dir != 0 or board_dir is None or board_dir >= 0:
        return artifact

    artifact["_shadow_flags"] = sorted(
        set(artifact.get("_shadow_flags") or []) | {"bearish_override_of_neutral_fundamental"}
    )
    _note(
        artifact,
        "SHADOW: board went bearish over a fundamental desk reporting no "
        "near-term view — the one override quadrant measured to lose money "
        "(-2.81%/decision, n=68; -2.38% executable-only at p=0.0015). "
        "Decision NOT altered; flagged so the population can be counted.",
    )
    logger.info(
        "[ArtifactValidator][SHADOW] bearish_override_of_neutral_fundamental — %s/%s",
        cycle_id or "?", ticker or "?",
    )
    try:
        from app.v3.telemetry import record_guardrail_firing

        record_guardrail_firing(
            "bearish_override_of_neutral_fundamental",
            ticker=ticker, cycle_id=cycle_id,
            detail={"shadow": True, "board_action": artifact.get("action")},
        )
    except Exception as e:
        logger.warning("[ArtifactValidator] shadow telemetry failed: %s", e)
    return artifact


_VALIDATORS = {
    "regime_classification": validate_regime_artifact,
    "desk_note": validate_desk_note_artifact,
    "fundamental_report": validate_fundamental_report_artifact,
    "valuation_report": validate_valuation_report_artifact,
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
