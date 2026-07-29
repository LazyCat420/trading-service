"""
Debate helpers — the surviving remnant of the classic Bull/Bear coordinator.

The adversarial debate engine this file was named for was deleted 2026-07-29
(see the retirement note below). What is left is three helpers that
``app/cognition/debate/tournament.py`` imports:

  * ``_cap_debate_text``          — context-budget trim
  * ``filter_packet_for_persona`` — evidence partitioning per persona
  * ``_build_evidence_header``    — EvidencePacket -> prompt header

The module keeps its name so the import in ``tournament.py`` stays valid and
the git history stays attached to one path. Renaming it would be a rename for
tidiness that breaks ``git log --follow`` on the part we kept.
"""

import logging

from app.config.context_budget import get_context_budget
from app.cognition.contracts.evidence import EvidencePacket

logger = logging.getLogger(__name__)


# ── Context Budget Cap for Debate Prompts ────────────────────────────
def _cap_debate_text(text: str, max_chars: int, label: str = "debate") -> str:
    """Truncate text to max_chars with a tail marker.

    Used to prevent opponent quotes and user prompts from exceeding
    the model's effective context window.
    """
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    marker = f"\n... [{label}: truncated from {len(text):,} to {max_chars:,} chars]"
    logger.info(
        "[DEBATE] %s truncated: %d -> %d chars",
        label,
        len(text),
        max_chars,
    )
    return truncated + marker


# ── Analyst Personas ─────────────────────────────────────────

PERSONAS = {
    "Fundamental": "Focus purely on valuation multiples, earnings trends, balance sheet health, ratios, and margins.",
    "Technical": "Focus purely on price action, moving averages, relative strength (RSI), volume patterns, and momentum indicators.",
    "Macro_Sentiment": "Focus purely on the broader macroeconomic regime, interest rates, industry catalysts, and social/news sentiment.",
}

# ── Evidence Partitioning — prevent cross-persona fact anchoring ──
PERSONA_EVIDENCE_FILTER: dict[str, list[str]] = {
    "Fundamental": [
        "pe_ratio", "earnings", "revenue", "margins", "debt", "fcf",
        "book_value", "dividend", "eps", "roe", "roa", "p_e", "p_b",
        "operating", "net_income", "balance", "cash_flow", "valuation",
        "fundamental", "financial", "ratio",
    ],
    "Technical": [
        "rsi", "sma", "ema", "volume", "macd", "bollinger", "atr",
        "moving_average", "momentum", "price", "close", "open", "high",
        "low", "support", "resistance", "trend", "technical", "indicator",
    ],
    "Macro_Sentiment": [
        "fed_rate", "sector_flow", "reddit_score",
        "interest_rate", "inflation", "gdp", "unemployment",
        "macro", "catalyst", "industry", "regime",
    ],
    # Added 2026-07-29 for the probabilistic panel's 4th analyst.
    #
    # The panel needs four DISJOINT slices; there were only three categories, so
    # the old tournament's four personas collapsed onto them — Momentum_Quant
    # and Volatility_Quant both mapped to "Technical" and read the identical
    # single fact. Four "independent" debaters, three views.
    #
    # Positioning is a genuinely separate question from macro: "who owns this
    # and who is buying" is not "what is the cycle doing". The keywords below
    # were MOVED out of Macro_Sentiment rather than duplicated — a fact
    # reachable from two slices is not partitioned, and the whole mechanism is
    # that each analyst holds evidence the others do not.
    "Positioning": [
        "congress", "institutional", "insider", "holdings", "ownership",
        "positioning", "flow", "short_interest", "options", "social",
        "news", "news_sentiment", "youtube", "retail",
    ],
}

# ── Per-Persona Temperature Diversity ────────────────────────
PERSONA_TEMPERATURES: dict[str, dict[str, float]] = {
    "Fundamental": {"bull": 0.3, "bear": 0.3},   # Low — be precise with numbers
    "Technical":   {"bull": 0.5, "bear": 0.5},   # Mid — pattern interpretation varies
    "Macro_Sentiment": {"bull": 0.7, "bear": 0.7},  # Higher — narrative/sentiment is fuzzy
}


#: Counts every time the evidence filter fell back to the full packet, keyed by
#: persona. A caller can read this to find out whether the debate it just ran
#: was actually held under information asymmetry, or silently degenerated into
#: N agents reading one packet. Reset per process; this is telemetry, not state.
PARTITION_FALLBACKS: dict[str, int] = {}


def filter_packet_for_persona(
    packet: EvidencePacket, persona_name: str,
) -> EvidencePacket:
    """Return a shallow copy of the evidence packet filtered to this persona's focus area.

    Each persona only sees facts whose fact_type matches its allowed keywords.
    This prevents cross-persona fact anchoring — Technical can't cite P/E because
    it never saw the data.

    **Why the partition matters, and why its failure is loud (2026-07-29).**
    Information asymmetry is not a nicety here, it is the mechanism. Given
    identical inputs, multi-agent debate is a martingale — expected correctness
    does not improve across rounds — and LLM errors are ~60% correlated, so N
    agents on one packet produce N correlated opinions and call the agreement
    consensus. Partitioned evidence is what makes the disagreement informative.

    Both fallbacks below therefore return the FULL packet, which is precisely
    the state that destroys the mechanism. This has already happened in
    production: the old ``fact_type`` names matched no Technical or Macro
    keyword, the filter fell back for **3 of 4** pitch personas, and the
    tournament produced four near-identical pitches from four "independent"
    debaters. Nothing failed; a warning was logged and the run looked healthy.

    The fallbacks are KEPT — a blind persona is worse than an over-informed one,
    and this must never raise inside a debate — but they now record themselves
    in ``PARTITION_FALLBACKS`` so a caller can assert the partition actually
    held. A degradation nothing can observe is indistinguishable from success.
    """
    allowed_keys = PERSONA_EVIDENCE_FILTER.get(persona_name)
    if not allowed_keys:
        # Unknown persona. Usually a name mismatch between the caller's persona
        # labels and PERSONA_EVIDENCE_FILTER's keys — exactly the defect that
        # forced tournament.py to carry its own PITCH_PERSONA_FILTER map.
        PARTITION_FALLBACKS[persona_name] = PARTITION_FALLBACKS.get(persona_name, 0) + 1
        logger.warning(
            "[DEBATE] No evidence filter defined for persona %r — using the FULL "
            "packet. This persona is not partitioned; the debate is that much "
            "closer to N agents reading one packet. Known personas: %s",
            persona_name, sorted(PERSONA_EVIDENCE_FILTER),
        )
        return packet

    filtered_facts = [
        f for f in packet.structured_facts
        if any(k in f.fact_type.lower() for k in allowed_keys)
    ]

    # If filtering removed ALL facts, fall back to full packet so the
    # persona isn't left completely blind (edge case: misclassified fact_types).
    if not filtered_facts and packet.structured_facts:
        PARTITION_FALLBACKS[persona_name] = PARTITION_FALLBACKS.get(persona_name, 0) + 1
        logger.warning(
            "[DEBATE] Evidence filter for %s matched 0/%d facts — using FULL "
            "packet. Its keywords %s match no fact_type present (%s), so this "
            "persona is unpartitioned.",
            persona_name,
            len(packet.structured_facts),
            sorted(allowed_keys),
            sorted({f.fact_type for f in packet.structured_facts})[:8],
        )
        return packet

    return packet.model_copy(update={"structured_facts": filtered_facts})


def partition_report(personas: list[str] | None = None) -> dict:
    """Summarise whether the evidence partition is actually holding.

    Returns ``{"fallbacks": {...}, "total": N, "partitioned": bool}``. A caller
    that runs a debate can log or assert on this: ``partitioned=False`` means at
    least one agent read the full packet and the run's "independent" views are
    not independent. Intended for the acceptance harness and for any future
    rebuild, which is worthless if the partition silently self-disables.
    """
    fallbacks = dict(PARTITION_FALLBACKS)
    if personas is not None:
        fallbacks = {k: v for k, v in fallbacks.items() if k in personas}
    total = sum(fallbacks.values())
    return {"fallbacks": fallbacks, "total": total, "partitioned": total == 0}




# ── RETIRED 2026-07-29: the classic adversarial debate engine ────────────────
#
# ``run_adversarial_debate`` and its helpers (``build_system_prompt``,
# ``_run_biased_agent``, ``_extract_claims_from_turns``) were deleted here:
# ~1,230 lines with NO production caller. The only importer was
# ``scripts/debug/test_pipeline_dataflow.py``, deleted with them.
#
# The tournament replaced this engine. Verified before deleting: the
# tournament's exception fallback (``orchestrator.py`` ~1365) queues
# ``bull_agent``/``bear_agent`` — the small modules in ``app/v3/agents/`` — and
# never re-enters this file. Nothing else referenced it.
#
# What remains is the only reason the file was still imported: three helpers
# used by ``tournament.py:36-40``. They stay HERE rather than moving to a new
# module, because extracting them would add a file to remove a file.
#
#   _cap_debate_text          — context-budget trim, pinned by
#                               tests/unit/test_context_governance.py
#   filter_packet_for_persona — evidence partitioning (information asymmetry)
#   _build_evidence_header    — renders an EvidencePacket into a prompt header
#
# Recover the engine from git history if it is ever wanted again; do not
# reconstruct it from memory.
def _build_evidence_header(packet: EvidencePacket) -> str:
    facts = {f.fact_type: f.value for f in packet.structured_facts}
    lines = ["## EVIDENCE FILE (pre-verified, cite directly):"]
    for k, v in facts.items():
        lines.append(f"  {k}: {v}")
    if getattr(packet, "tool_cache", None):
        lines.append("## PRE-FETCHED TOOL DATA:")
        for tool_name, result in packet.tool_cache.items():
            lines.append(f"  [{tool_name}]: {result[:500]}")
    return "\n".join(lines)


USER_TEMPLATE = """## Entity: {entity_id}

{position_block}
## Structured Facts:
{structured_facts}

## Unstructured Context (Reddit/YouTube/News):
{unstructured_context}

## Available Claims from Evidence:
{claims_text}

## Missing Data:
{missing_fields}

## Specialist Agent Insights:
{agent_insights}

Construct your case based ONLY on the data above. Cite specific values with [source:value] format."""


CROSS_EXAM_USER_TEMPLATE = """## BULL ANALYST CLAIMS:
{bull_claims}

## BEAR ANALYST CLAIMS:
{bear_claims}

## IN-DEBATE TOOL RESEARCH (Ground Truth from Agent Tools):
{tool_research}

## UNSTRUCTURED CONTEXT (News, Reddit, YouTube):
{unstructured_context}

## ACTUAL STRUCTURED FACTS (ground truth):
{structured_facts}

Cross-examine both sets of claims against the actual data, context, AND the in-debate tool research above.
NOTE: Be highly tolerant of minor decimal rounding differences (e.g. 31.54 vs 31.539) and shorthand notations (e.g. $81.3B vs 81300000000.0). Do not flag these as unverified if the values represent the same underlying data point."""


