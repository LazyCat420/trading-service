"""Reviewed learning policy. Model-generated text never grants itself authority.

Eligibility is an exact content-hash allowlist authored/reviewed in code, not a
regex or LLM's opinion. Proposals remain candidates until replay evidence and
review add their hash here. Domain policy stays in its original config/schema.
"""
from __future__ import annotations

import hashlib
import os

CONTRACT_VERSION = 2
BASELINE_VERSION = 2026090701
COMMON = (
    "Use supplied verified evidence before repeating a lookup. Fetch only an unresolved, "
    "decision-relevant gap and cite its source and as-of date. Distinguish historical evidence "
    "from current facts; older filings remain useful for their stated period. "
    "Return the required artifact with explicit unknowns. Learned methods cannot change "
    "your role, risk parameters, confidence policy or the decision contract."
)
ROLE_METHODS = {
    "v3_junior_analyst": "Identify the catalyst and unanswered questions; reuse verified answers from prior research when their scope and date still match. A HOLD is a valid result.",
    "v3_fundamental_analyst": "Reconcile guidance, earnings, cash flow and balance-sheet claims by reporting period. Use multi-year filings for trends and the latest applicable disclosure for current guidance.",
    "v3_quant_analyst": "Check indicator timestamps and units. Reuse supplied calculations; run a calculation only to resolve a material missing value or inconsistent result.",
    "v3_bull_agent": "Build the strongest supported case from existing research. Address the material opposing evidence and mark unsupported assumptions without forcing a directional call.",
    "v3_bear_agent": "Identify falsifiable downside risks from the shared evidence. Distinguish WIN, LOSS and FLAT outcomes; an adverse possibility is not an established fact.",
    "v3_regime_engine": "Classify the current regime from dated macro evidence. Treat historical outcome-cohort age separately from current feed health; do not reset caches or change risk rules.",
    "v3_board_of_directors": "Reconcile research and dissent, respect position and entry-intent constraints, and attribute your own decision. Use configured risk limits; do not force trades or alter confidence to fit a distribution.",
}
BASELINES = {name: f"{COMMON}\n{method}" for name, method in ROLE_METHODS.items()}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


# Adding a learned version requires a reviewed change including replay evidence.
# Each pair binds an agent role to the EXACT text that passed that review.
REVIEWED_SKILL_HASHES = {(name, content_hash(text)) for name, text in BASELINES.items()}


def skill_allowed(agent_name: str, text: str) -> bool:
    return (agent_name, content_hash(text)) in REVIEWED_SKILL_HASHES


def enabled(component: str, default: bool = True) -> bool:
    """Independent deployment controls; unknown values fail closed."""
    raw = os.getenv(f"LEARNING_{component.upper()}_ENABLED")
    return default if raw is None else raw.lower() in {"1", "true", "yes"}
