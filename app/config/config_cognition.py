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
    TOURNAMENT_MODE: bool = True  # 4-stage tournament debate (pitch → backtest → h2h → jury)
    # Tournament cost controls (T4/T5). Both default OFF so behavior is unchanged
    # until explicitly enabled.
    #   FAST_MODE: drop pitch personas 4→2 (Value+Momentum) and jury 3→1 (Risk
    #   Manager) for non-core tickers — ~half the tournament LLM calls.
    #   JURY_ON_JETSON: route the 3 jury scoring calls to the lightweight Jetson
    #   vLLM endpoint (via the "consensus" routing keyword). Leave OFF unless the
    #   Jetson endpoint is confirmed live — a disabled endpoint makes jurors
    #   silently fall back to the default score.
    TOURNAMENT_FAST_MODE: bool = False
    TOURNAMENT_JURY_ON_JETSON: bool = False

    # Layer 4: Reflective Memory (Dev 5)
    ENABLE_REFLECTIVE_MEMORY: bool = True

    # Specific Feature Flags for Dev 2
    ENABLE_LLM_CLAIM_ENRICHMENT: bool = True

    # V3 Family Office Architecture (Baron Funds Model)
    V3_FAMILY_OFFICE_ENABLED: bool = False  # Feature flag — enables CIO-driven dynamic debate
    V3_MAX_CIO_LOOPS: int = 3               # Hard-stop guardrail — max debate rounds
    V3_PM_TIMEOUT_SECONDS: int = 120        # Per-PM analysis timeout
    V3_WORKER_TIMEOUT_SECONDS: int = 60     # Per-worker data fetch timeout
    V3_ABSTAIN_ON_MAX_LOOPS: bool = True    # ABSTAIN vs forced decision on max loops

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
