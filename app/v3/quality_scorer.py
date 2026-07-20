"""
V3 Quality Scorer — Artifact quality assessment for dead end detection.

Scores each agent artifact on a 0-100 scale across 4 dimensions:
  1. Content Density — Is the output substantive or boilerplate?
  2. Data Completeness — Were optional fields populated?
  3. Consistency — Do numbers/directions agree with each other?
  4. Source Grounding — Are claims backed by specific data?

The composite score powers the "yellow node" dead end detection in
the Pipeline Replay Dashboard.

Usage:
    from app.v3.quality_scorer import score_artifact
    result = score_artifact("desk_note", artifact_dict)
    # result = {"quality_score": 72, "content_density": 85, ...}
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Boilerplate patterns that indicate empty/fallback outputs ──
_BOILERPLATE_PATTERNS = [
    r"unable to (?:analyze|assess|evaluate|determine)",
    r"no (?:data|information) (?:available|found|provided)",
    r"insufficient (?:data|information)",
    r"not (?:analyzed|available|applicable)",
    r"skipped (?:detailed|due to)",
    r"could not (?:retrieve|obtain|find|access)",
    r"n/?a",
    r"placeholder",
    r"tbd",
    r"unknown",
    r"error (?:occurred|during|while)",
    r"failed to (?:retrieve|fetch|load|parse)",
]
_BOILERPLATE_RE = re.compile(
    "|".join(f"(?:{p})" for p in _BOILERPLATE_PATTERNS),
    re.IGNORECASE,
)

# ── Grounding indicators — signs the content references real data ──
_GROUNDING_PATTERNS = [
    r"\$\d",                    # Dollar amounts ($123)
    r"\d+\.\d+%",              # Percentages (12.5%)
    r"(?:Q[1-4]|FY)\s*\d{2,4}", # Fiscal quarters (Q3 2025)
    r"\b(?:yfinance|finnhub|reddit|tavily|exa|bing|rss|youtube)\b",  # Tool sources
    r"\b(?:revenue|earnings|EPS|P/E|EBITDA|FCF|margin)\b",  # Financial metrics
    r"\b(?:RSI|SMA|EMA|ATR|VWAP|bollinger|fibonacci)\b",   # Technical indicators
    r"\b(?:VIX|DXY|yield|treasury|fed)\b",                 # Macro indicators
    r"\b20\d{2}\b",             # Year references (2024, 2025)
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",  # Month names
]
_GROUNDING_RE = re.compile(
    "|".join(f"(?:{p})" for p in _GROUNDING_PATTERNS),
    re.IGNORECASE,
)


def score_artifact(artifact_type: str, artifact: dict) -> dict[str, Any]:
    """Score an artifact's quality across 4 dimensions.

    Args:
        artifact_type: The artifact schema key (e.g. "desk_note")
        artifact: The validated artifact dict

    Returns:
        Dict with quality_score (0-100) and per-dimension scores.
    """
    if not artifact:
        return _empty_result("Empty artifact")

    scores = {
        "content_density": _score_content_density(artifact),
        "data_completeness": _score_data_completeness(artifact_type, artifact),
        "consistency": _score_consistency(artifact_type, artifact),
        "source_grounding": _score_source_grounding(artifact),
    }

    # Weighted composite — content density matters most
    weights = {
        "content_density": 0.35,
        "data_completeness": 0.25,
        "consistency": 0.20,
        "source_grounding": 0.20,
    }

    composite = sum(scores[k] * weights[k] for k in weights)
    composite = max(0, min(100, int(round(composite))))

    # Flag classification
    if composite >= 70:
        flag = "good"
    elif composite >= 40:
        flag = "weak"
    else:
        flag = "dead_end"

    # Detect specific failure patterns
    failure_patterns = _detect_failure_patterns(artifact_type, artifact, scores)

    return {
        "quality_score": composite,
        "flag": flag,
        **scores,
        "failure_patterns": failure_patterns,
    }


def _empty_result(reason: str) -> dict[str, Any]:
    return {
        "quality_score": 0,
        "flag": "dead_end",
        "content_density": 0,
        "data_completeness": 0,
        "consistency": 0,
        "source_grounding": 0,
        "failure_patterns": [reason],
    }


# ═══════════════════════════════════════════════════════════════════
# Dimension 1: Content Density
# ═══════════════════════════════════════════════════════════════════

def _score_content_density(artifact: dict) -> int:
    """Score based on the substantiveness of text content."""
    # Gather all text fields
    text_fields = []
    for key in ("summary", "reasoning", "rationale", "analysis", "content"):
        val = artifact.get(key)
        if isinstance(val, str) and val.strip():
            text_fields.append(val.strip())

    # The tournament's substance is in the theses it argued, not in a summary
    # field — scoring only `rationale` would judge a 4-stage debate by its
    # one-line verdict.
    for list_field in ("pitches", "survivors"):
        for item in artifact.get(list_field) or []:
            if isinstance(item, dict):
                claim = item.get("claim")
                if isinstance(claim, str) and claim.strip():
                    text_fields.append(claim.strip())
    h2h = artifact.get("h2h")
    if isinstance(h2h, dict):
        for side in ("thesis_a", "thesis_b"):
            thesis = h2h.get(side)
            if isinstance(thesis, dict):
                claim = thesis.get("claim")
                if isinstance(claim, str) and claim.strip():
                    text_fields.append(claim.strip())
                for point in thesis.get("attack_points") or []:
                    if isinstance(point, str) and point.strip():
                        text_fields.append(point.strip())

    if not text_fields:
        return 0

    text = " ".join(text_fields)
    char_count = len(text)
    word_count = len(text.split())

    # Check for boilerplate
    boilerplate_matches = _BOILERPLATE_RE.findall(text)
    boilerplate_ratio = len(boilerplate_matches) / max(1, word_count / 10)

    # Score components
    score = 0

    # Length scoring (0-40 points)
    if char_count < 50:
        score += 0
    elif char_count < 100:
        score += 10
    elif char_count < 300:
        score += 25
    elif char_count < 800:
        score += 35
    else:
        score += 40

    # Unique word ratio (0-30 points) — repetitive text scores low
    words = text.lower().split()
    if words:
        unique_ratio = len(set(words)) / len(words)
        score += int(unique_ratio * 30)

    # Boilerplate penalty (0 to -30 points)
    if boilerplate_ratio > 0.5:
        score -= 30
    elif boilerplate_ratio > 0.2:
        score -= 15
    elif boilerplate_ratio > 0:
        score -= 5

    # Sentence count bonus (0-10 points) — multi-sentence = structured thinking
    sentences = re.split(r'[.!?]+', text)
    sentence_count = len([s for s in sentences if len(s.strip()) > 10])
    score += min(10, sentence_count * 2)

    return max(0, min(100, score))


# ═══════════════════════════════════════════════════════════════════
# Dimension 2: Data Completeness
# ═══════════════════════════════════════════════════════════════════

# Expected optional fields per artifact type
_OPTIONAL_FIELDS: dict[str, list[str]] = {
    "desk_note": ["leads_to_trace", "data_gaps", "key_findings"],
    "fundamental_report": ["catalysts", "risks", "data_gaps", "pillars"],
    "quant_report": ["position_sizing_note", "stop_loss_suggestion", "data_gaps", "risk_metrics"],
    "bull_argument": ["claims", "target_upside"],
    "bear_rebuttal": ["rebuttals", "independent_risks", "target_downside"],
    "bull_defense": ["defense_points", "concessions"],
    "debate_judge": [
        "verified_bull_claims", "unverified_bull_claims",
        "verified_bear_claims", "unverified_bear_claims",
    ],
    "regime_classification": ["rationale", "vix_level", "yield_trend", "dxy_trend"],
    "final_decision": ["position_size_pct", "stop_loss", "take_profit", "persona_used", "regime"],
    "trade_decision": ["signal_weights", "signal_assessments", "risk_flags", "stop_loss", "take_profit"],
    "tournament_debate": ["pitches", "survivors", "h2h", "jury_verdict", "risk_flags"],
}


def _score_data_completeness(artifact_type: str, artifact: dict) -> int:
    """Score based on how many optional fields were populated."""
    optional = _OPTIONAL_FIELDS.get(artifact_type, [])
    if not optional:
        return 70  # Unknown type, neutral score

    filled = 0
    for field in optional:
        val = artifact.get(field)
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        if isinstance(val, (list, dict)) and not val:
            continue
        filled += 1

    ratio = filled / len(optional)

    # Non-linear scoring: first few fields matter most
    if ratio >= 0.8:
        return 95
    elif ratio >= 0.6:
        return 75
    elif ratio >= 0.4:
        return 55
    elif ratio >= 0.2:
        return 35
    else:
        return 10


# ═══════════════════════════════════════════════════════════════════
# Dimension 3: Consistency
# ═══════════════════════════════════════════════════════════════════

def _score_consistency(artifact_type: str, artifact: dict) -> int:
    """Score based on internal consistency of the artifact."""
    issues = 0
    checks = 0

    confidence = artifact.get("confidence")
    summary = (artifact.get("summary") or artifact.get("reasoning") or "").lower()

    # Check 1: Confidence vs. language consistency
    if isinstance(confidence, (int, float)) and summary:
        checks += 1
        low_conf_words = ["limited", "insufficient", "unable", "uncertain", "unclear", "lack"]
        high_conf_words = ["strong", "compelling", "clear", "significant", "robust", "substantial"]

        has_low_language = any(w in summary for w in low_conf_words)
        has_high_language = any(w in summary for w in high_conf_words)

        if confidence > 75 and has_low_language and not has_high_language:
            issues += 1  # High confidence but uncertain language
        elif confidence < 35 and has_high_language and not has_low_language:
            issues += 1  # Low confidence but certain language

    # Check 2: thesis_direction vs. confidence
    thesis = artifact.get("thesis_direction", "")
    if thesis and isinstance(confidence, (int, float)):
        checks += 1
        if thesis in ("BULLISH", "BEARISH") and confidence < 30:
            issues += 1  # Strong direction but very low confidence

    # Check 3: Action vs. confidence
    action = artifact.get("action", "")
    if action and isinstance(confidence, (int, float)):
        checks += 1
        if action in ("BUY", "SELL") and confidence < 40:
            issues += 1  # Trade action with very low confidence

    # Check 4: Claims/rebuttals have actual content
    for list_field in ("claims", "rebuttals", "key_findings", "defense_points"):
        items = artifact.get(list_field)
        if isinstance(items, list) and items:
            checks += 1
            empty_items = sum(1 for item in items if _is_empty_item(item))
            if empty_items > len(items) * 0.5:
                issues += 1  # More than half of list items are empty

    # Check 5: data_gaps honesty — low confidence should have data_gaps
    data_gaps = artifact.get("data_gaps", [])
    if isinstance(confidence, (int, float)) and confidence < 50:
        checks += 1
        if not data_gaps:
            issues += 1  # Low confidence but claims no data gaps

    # Check 6: tournament stage integrity. The tournament is the most
    # expensive stage in the pipeline (~264s/ticker, ~1.2M tokens per 5-ticker
    # cycle) and its failure mode is NOT weak prose — it is stages silently
    # collapsing: every pitch dropped before the H2H, an empty jury, or the
    # fallback path returning HOLD@0. Text-shaped checks cannot see any of
    # that, which is why this artifact went unscored (-1) while burning a
    # third of the cycle's agent time.
    if artifact_type == "tournament_debate":
        pitches = artifact.get("pitches") or []
        survivors = artifact.get("survivors") or []
        h2h = artifact.get("h2h") or {}
        jury = artifact.get("jury_verdict") or {}

        # Stage 1 → 2: pitches must exist and some must survive.
        checks += 1
        if not pitches:
            issues += 1
        checks += 1
        if pitches and not survivors:
            issues += 1  # every thesis eliminated — no debate happened

        # Stage 3: head-to-head needs two named theses to be a real debate.
        checks += 1
        if not (h2h.get("thesis_a", {}).get("claim")
                and h2h.get("thesis_b", {}).get("claim")):
            issues += 1

        # Stage 4: a verdict with no jury is an unearned answer.
        checks += 1
        if not jury:
            issues += 1

        # The fallback path returns winning_side="fallback" with HOLD@0 —
        # a structurally valid dict that did no work.
        checks += 1
        if artifact.get("winning_side") == "fallback":
            issues += 1

    if checks == 0:
        return 70  # No checkable fields, neutral score

    issue_ratio = issues / checks
    if issue_ratio == 0:
        return 95
    elif issue_ratio <= 0.2:
        return 75
    elif issue_ratio <= 0.4:
        return 55
    elif issue_ratio <= 0.6:
        return 35
    else:
        return 15


def _is_empty_item(item: Any) -> bool:
    """Check if a list item is effectively empty."""
    if item is None:
        return True
    if isinstance(item, str):
        return len(item.strip()) < 5
    if isinstance(item, dict):
        return all(
            not v or (isinstance(v, str) and len(v.strip()) < 5)
            for v in item.values()
        )
    return False


# ═══════════════════════════════════════════════════════════════════
# Dimension 4: Source Grounding
# ═══════════════════════════════════════════════════════════════════

def _score_source_grounding(artifact: dict) -> int:
    """Score based on whether claims reference specific data/sources."""
    # Gather all text content
    all_text = []
    for key, val in artifact.items():
        if isinstance(val, str):
            all_text.append(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    all_text.append(item)
                elif isinstance(item, dict):
                    for v in item.values():
                        if isinstance(v, str):
                            all_text.append(v)

    text = " ".join(all_text)
    if not text.strip():
        return 0

    # Count grounding indicators
    matches = _GROUNDING_RE.findall(text)
    word_count = len(text.split())

    # Grounding density: matches per 100 words
    density = (len(matches) / max(1, word_count)) * 100

    if density >= 5:
        return 95  # Very well-grounded
    elif density >= 3:
        return 80
    elif density >= 1.5:
        return 65
    elif density >= 0.5:
        return 45
    elif density > 0:
        return 25
    else:
        return 5  # Pure opinion, no data references


# ═══════════════════════════════════════════════════════════════════
# Failure Pattern Detection
# ═══════════════════════════════════════════════════════════════════

def _detect_failure_patterns(
    artifact_type: str,
    artifact: dict,
    scores: dict[str, int],
) -> list[str]:
    """Detect specific named failure patterns for logging/display."""
    patterns = []

    # Pattern: Fallback Output — agent returned safe defaults
    summary = artifact.get("summary", "")
    if isinstance(summary, str) and _BOILERPLATE_RE.search(summary):
        patterns.append("FALLBACK_OUTPUT")

    # Pattern: Confidence-Content Mismatch
    confidence = artifact.get("confidence")
    if isinstance(confidence, (int, float)):
        if confidence > 70 and scores.get("content_density", 100) < 40:
            patterns.append("HIGH_CONFIDENCE_LOW_CONTENT")
        if confidence < 30 and scores.get("content_density", 0) > 70:
            patterns.append("LOW_CONFIDENCE_HIGH_CONTENT")

    # Pattern: Empty Arrays — required list fields present but empty
    for field in ("key_findings", "claims", "rebuttals", "risks", "catalysts"):
        val = artifact.get(field)
        if isinstance(val, list) and len(val) == 0:
            if field in ("key_findings", "claims", "rebuttals"):
                patterns.append(f"EMPTY_{field.upper()}")

    # Pattern: Cascading Data Gap — high data_gaps count with low confidence
    data_gaps = artifact.get("data_gaps", [])
    if isinstance(data_gaps, list) and len(data_gaps) >= 3:
        if isinstance(confidence, (int, float)) and confidence < 50:
            patterns.append("CASCADING_DATA_GAPS")

    # Pattern: Dummy/Stub artifact (HIGH_VOLATILITY skip)
    if artifact_type == "fundamental_report":
        if summary and "skipped" in summary.lower():
            patterns.append("REGIME_SKIP")

    return patterns
