"""
V3 Contradiction Shadow — observation-only cross-artifact dissent detector.

This is the *shadow-mode* first step of the peer-to-peer mesh evolution. It
reuses the previously-dead cognition primitives (Claim / cluster_claims /
detect_contradictions) that were written but never wired into V3, and runs
them across the SharedDesk artifacts at the end of a cycle.

It NEVER changes a decision. It only:
  1. extracts directional (sentiment) and price-target claims from the desk,
  2. clusters them and runs the existing contradiction detector,
  3. records a structured report onto the desk telemetry + returns it.

The point is to answer, empirically, before we build the real mesh:
  "How often do the analysts / debate verdict / board / synthesizer actually
   contradict each other, and how often would a contradiction gate have
   downgraded a live BUY/SELL to HOLD?"

If the answer is "rarely and it wouldn't change decisions", we've saved
ourselves the whole request/response protocol. If it's "often", we have the
objective, non-gameable trigger the threshold-on-quality-score plan lacked.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Artifacts whose *direction* is a genuine independent assessment of the
# ticker. Deliberately EXCLUDES bull_argument / bear_rebuttal — those are
# adversarial by construction (the bull is always bullish), so counting them
# would flag a "contradiction" on every single cycle and tell us nothing.
_SENTIMENT_DIRECTION_FIELDS = (
    "fundamental_report",
    "quant_report",
)

# Resolved verdicts carry an action (BUY/SELL/HOLD) rather than a
# thesis_direction; map the action into the same BULLISH/BEARISH space.
_SENTIMENT_ACTION_ARTIFACTS = (
    "tournament_result",
    "final_decision",   # Board of Directors
    "trade_decision",   # Decision Synthesizer
)

_ACTION_TO_SENTIMENT = {
    "BUY": "BULLISH",
    "LONG": "BULLISH",
    "SELL": "BEARISH",
    "SHORT": "BEARISH",
    "HOLD": "NEUTRAL",
    "PASS": "NEUTRAL",
}

# Fields that are unambiguously an absolute price target in the same units
# ($/share). Percent-based fields (target_upside) are excluded so we never
# compare a "12" that means 12% against a "150" that means $150.
_PRICE_TARGET_FIELDS = ("take_profit", "price_target")


def _norm_direction(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    v = val.strip().upper()
    if v in ("BULLISH", "BEARISH", "NEUTRAL"):
        return v
    return None


def _norm_action(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    return _ACTION_TO_SENTIMENT.get(val.strip().upper())


def _make_claim(
    *,
    ticker: str,
    predicate: str,
    object_value: str,
    source_label: str,
    confidence_0_100: Any,
):
    """Build a cognition Claim from a desk artifact field."""
    from app.cognition.contracts.claims import Claim, Provenance

    try:
        conf = float(confidence_0_100)
    except (TypeError, ValueError):
        conf = 50.0
    conf = max(0.0, min(1.0, conf / 100.0))

    return Claim(
        id=str(uuid.uuid4()),
        subject_entity_id=ticker,
        predicate=predicate,
        object_value=object_value,
        claim_type="inference",
        origin="llm_inferred",
        source_ids=[source_label],
        timestamp=datetime.now(timezone.utc),
        confidence=conf,
        freshness_score=1.0,
        provenance=Provenance(
            source_table="shared_desk",
            source_id=source_label,
            extraction_method="contradiction_shadow",
        ),
    )


def _extract_claims(desk) -> list:
    """Pull sentiment + price-target claims off the desk's artifacts."""
    claims: list = []
    ticker = desk.ticker or "UNKNOWN"

    # 1. Directional sentiment from independent analysts
    for atype in _SENTIMENT_DIRECTION_FIELDS:
        art = getattr(desk, atype, None)
        if not isinstance(art, dict):
            continue
        direction = _norm_direction(art.get("thesis_direction"))
        if direction and direction != "NEUTRAL":
            claims.append(_make_claim(
                ticker=ticker,
                predicate="sentiment",
                object_value=direction,
                source_label=atype,
                confidence_0_100=art.get("confidence", 50),
            ))

    # 2. Directional sentiment from resolved verdicts (action → sentiment)
    for atype in _SENTIMENT_ACTION_ARTIFACTS:
        art = getattr(desk, atype, None)
        if not isinstance(art, dict):
            continue
        # tournament_result / final_decision / trade_decision all expose "action"
        sentiment = _norm_action(art.get("action"))
        if sentiment and sentiment != "NEUTRAL":
            claims.append(_make_claim(
                ticker=ticker,
                predicate="sentiment",
                object_value=sentiment,
                source_label=atype,
                confidence_0_100=art.get("confidence", 50),
            ))

    # 3. Absolute price targets (same units) from wherever they appear
    price_sources = _SENTIMENT_DIRECTION_FIELDS + _SENTIMENT_ACTION_ARTIFACTS
    for atype in price_sources:
        art = getattr(desk, atype, None)
        if not isinstance(art, dict):
            continue
        for field in _PRICE_TARGET_FIELDS:
            raw = art.get(field)
            try:
                if raw is None:
                    continue
                price = float(raw)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            claims.append(_make_claim(
                ticker=ticker,
                predicate="price_target",
                object_value=str(price),
                source_label=f"{atype}.{field}",
                confidence_0_100=art.get("confidence", 50),
            ))

    return claims


def compute_contradiction_shadow(desk) -> dict[str, Any]:
    """Run the (previously-dead) contradiction detector across the desk.

    Returns a JSON-serializable shadow report. Never raises — on any failure
    it returns a report with an "error" key so the caller can log and move on.
    Purely observational: does NOT mutate the decision.
    """
    from app.cognition.evidence.clustering import cluster_claims
    from app.cognition.evidence.contradiction_detector import detect_contradictions

    report: dict[str, Any] = {
        "kind": "contradiction_shadow",
        "agent": "contradiction_shadow",
        # Telemetry-writer contract keys — persist_telemetry falls back to
        # "?" for missing agent_name/phase/outcome, which rendered this
        # entry as an anonymous "? 0.0s ⚠" node in the replay flow graph.
        "agent_name": "contradiction_shadow",
        "phase": "post_decision",
        "outcome": "SUCCESS",
        "elapsed_ms": 0,
        "ticker": desk.ticker,
        "shadow_only": True,
    }

    try:
        claims = _extract_claims(desk)
        report["claims_extracted"] = len(claims)

        # Human-readable map of who said what (directional only)
        sentiment_map = {
            c.source_ids[0]: c.object_value
            for c in claims
            if c.predicate == "sentiment"
        }
        report["sentiment_by_source"] = sentiment_map

        clusters = cluster_claims(claims)
        contradictions = detect_contradictions(clusters)

        report["contradictions"] = [
            {
                "description": c.description,
                "source_ref_1": c.source_ref_1,
                "source_ref_2": c.source_ref_2,
                "severity": c.severity,
            }
            for c in contradictions
        ]
        report["contradiction_count"] = len(contradictions)

        has_directional_conflict = any(
            "sentiment" in c.description.lower() or "sentiment" == getattr(cl.claims[0], "predicate", "")
            for c, cl in zip(contradictions, clusters)
        ) or "BULLISH" in sentiment_map.values() and "BEARISH" in sentiment_map.values()

        # The shadow metric: what the mesh's contradiction gate WOULD have done.
        # A directional conflict paired with a live trade action is exactly the
        # case a "downgrade-to-HOLD on unresolved dissent" gate would catch.
        final = desk.trade_decision or desk.final_decision or {}
        final_action = str(final.get("action", "")).upper()
        report["final_action"] = final_action
        report["final_confidence"] = final.get("confidence")
        report["would_downgrade_to_hold"] = bool(
            len(contradictions) > 0
            and has_directional_conflict
            and final_action in ("BUY", "SELL", "LONG", "SHORT")
        )

        if contradictions:
            logger.warning(
                "[ShadowContradiction] %s/%s: %d contradiction(s) detected "
                "(sentiment map=%s, final=%s@%s) would_downgrade=%s",
                (desk.cycle_id or "?")[:12], desk.ticker,
                len(contradictions), sentiment_map, final_action,
                report.get("final_confidence"), report["would_downgrade_to_hold"],
            )
        else:
            logger.info(
                "[ShadowContradiction] %s/%s: no contradiction across %d claims "
                "(sentiment map=%s)",
                (desk.cycle_id or "?")[:12], desk.ticker,
                len(claims), sentiment_map,
            )
    except Exception as e:  # noqa: BLE001 — shadow must never break a cycle
        logger.warning(
            "[ShadowContradiction] %s: computation failed (non-fatal): %s",
            desk.ticker, e,
        )
        report["error"] = str(e)

    return report
