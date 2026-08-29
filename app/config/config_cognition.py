"""
Configuration flags for the Cognition V2 multi-agent architecture.
Controls the staged rollout of new v2 components alongside the legacy pipeline.
"""

from pydantic_settings import BaseSettings


class CognitionSettings(BaseSettings):
    # Core V2 toggles
    ENABLE_COGNITION_V2: bool = False
    COGNITION_V2_STAGE: int = 4

    # Layer 1: Ontology & Graph (Dev 1)
    ENABLE_ONTOLOGY_GRAPH: bool = True

    # Layer 2: Evidence Fusion & Verification (Dev 2)
    ENABLE_EVIDENCE_FUSION: bool = True
    ENABLE_VERIFICATION_GATE: bool = True

    # Layer 3: Debate & Adjudication (Dev 3)
    ENABLE_DEBATE_REFINEMENT: bool = True
    DEBATE_ENABLED: bool = True  # toggle adversarial bull/bear debate
    DEBATE_MAX_TOOL_TURNS: int = 3  # max tool-calling turns per debate agent (allows verify, counter, conclude)
    CLAIM_REJECT_THRESHOLD: int = 8  # max unverified claims before LOW_INTEGRITY (3 personas × 4 turns = 24 agent turns)
    FAST_DEBATE_MODE: bool = True  # Halve debate latency with capped prompt sizes
    MAX_DEBATE_HISTORY_AGE_HOURS: int = 4  # Don't use debates older than this for context
    CONFIRMATION_LOOP_THRESHOLD: int = 3  # Force skepticism if N+ consecutive same verdicts
    #   MODEL_SHADOW_AGENTS: comma-separated agent names whose prompt is ALSO
    #   sent to MODEL_SHADOW_ENDPOINT after the primary call returns, purely to
    #   benchmark that box. The shadow answer is recorded in model_shadow_runs
    #   and never reaches a decision. Empty = off.
    #
    #   Pick candidates by LOOP COUNT, not prompt size: since Jetson went to
    #   65k every v3 role's largest single call fits, so what separates a good
    #   Jetson job from a bad one is how many times the ~1.9x slower box is hit
    #   serially. v3_regime_engine averages 1.1 loops (+~2.6s); the 7-8 loop
    #   agentic roles would pay ~+20s and one of them (v3_fundamental_analyst)
    #   overflows 65k anyway once Qwen's ~1.39x heavier tokenization is applied.
    MODEL_SHADOW_AGENTS: str = ""
    MODEL_SHADOW_ENDPOINT: str = "jetson"

    # Layer 4: Reflective Memory (Dev 5)
    ENABLE_REFLECTIVE_MEMORY: bool = True

    # Specific Feature Flags for Dev 2
    ENABLE_LLM_CLAIM_ENRICHMENT: bool = True

    # V3 Family Office Architecture (Baron Funds Model) — REMOVED 2026-07-29.
    # Five settings for a CIO-driven debate that was never built: the flag was
    # False and all five had ZERO readers anywhere in app/. A config entry with
    # no reader is worse than dead code — it reads as a working feature you
    # could switch on, and someone eventually tries.

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


cognition_settings = CognitionSettings()

# Static Data - Not overridable via environment variables
# NOTE: Per-persona debate temperatures are defined in PERSONA_TEMPERATURES
# (debate_coordinator.py) and passed explicitly — not looked up by agent_name.
LLM_TEMPERATURES = {
    "thesis_generation": 0.5,
    "debate": 0.7,
    "creative": 0.8,
    "factual": 0.0,
    # Adversarial debate support agents (not persona agents — those use PERSONA_TEMPERATURES)
    "cross_examiner": 0.2,
    "debate_judge": 0.2,
    "thesis_synthesis": 0.3,
}

# The default tools given to any worker spawned via Prism `create_team`
# These are the baseline survival tools; workers can dynamically acquire
# others (like polygon price history) using discover_and_enable_tools.
CORE_WORKER_TOOLS = [
    "read_memory_note",
    "write_memory_note",
    "search_web",
    "get_market_data",
    "get_finnhub_news",
    "search_internal_database",
]
