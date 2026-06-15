"""
TickerContext — shared state passed between pipeline steps.

Instead of runner.py passing 15+ local variables between inline code blocks,
each step reads from and writes to this context object. This makes the
data flow explicit and each step independently testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class TickerContext:
    """Shared context carried through all pipeline steps for one ticker."""

    # ── Identity ──
    ticker: str
    cycle_id: str
    bot_id: str

    # ── Callbacks ──
    emit: Callable[..., Any] = field(repr=False, default=None)

    # ── Timing ──
    start_time: float = field(default_factory=time.monotonic)
    stages: list[str] = field(default_factory=list)
    stage_timings: dict[str, int] = field(default_factory=dict)
    total_tokens: int = 0

    # ── Pipeline inputs ──
    macro_memo: str = ""
    watchlist: list[str] = field(default_factory=list)
    thesis_semaphore: Any = None  # asyncio.Semaphore
    db_semaphore: Any = None  # asyncio.Semaphore
    is_highly_redundant: bool = False
    research_focus: str = ""
    trigger_type: str = "manual"

    # ── Step 0: Position context ──
    position_context: dict[str, Any] = field(default_factory=dict)
    portfolio_dashboard: str = ""
    held: bool = False

    # ── Step 0.5-0.6: Data completeness ──
    data_report: dict[str, Any] = field(default_factory=dict)

    # ── Step 1: Ontology ──
    ontology_ctx: dict[str, Any] = field(default_factory=dict)

    # ── Step 2: Evidence packet ──
    packet: Any = None  # EvidencePacket

    # ── Step 3: Sufficiency ──
    sufficiency: Any = None  # SufficiencyResult
    retrieval_retries: int = 0

    # ── Step 5: Memory ──
    memory_context: dict[str, Any] = field(default_factory=dict)

    # ── Step 5.5: Agent insights ──
    agent_insights: dict[str, Any] = field(default_factory=dict)
    orchestrator_had_agents: bool = False

    # ── Step 5.65: Team findings ──
    team_findings_summary: str = ""

    # ── Step 5.7: Debate ──
    debate_result: Any = None  # DebateResult

    # ── Step 6: Thesis ──
    thesis: Any = None
    thesis_tokens: int = 0

    # ── Step 6.5: Hallucination check ──
    hallucination_result: dict[str, Any] | None = None

    # ── Final decision ──
    final_action: str = "HOLD"
    final_confidence: int = 0
    final_rationale: str = ""
    failure_diagnosis: dict[str, Any] | None = None

    # ── Helpers ──

    def add_stage(self, name: str, elapsed_ms: int = 0) -> None:
        """Record a completed stage with timing."""
        self.stages.append(name)
        if elapsed_ms:
            self.stage_timings[name] = elapsed_ms

    def add_tokens(self, count: int) -> None:
        """Accumulate token usage."""
        self.total_tokens += count

    def elapsed_ms(self, since: float | None = None) -> int:
        """Milliseconds since start or since a given timestamp."""
        ref = since if since is not None else self.start_time
        return int((time.monotonic() - ref) * 1000)

    def elapsed_s(self) -> float:
        """Seconds since pipeline start."""
        return time.monotonic() - self.start_time

    def safe_emit(self, *args: Any, **kwargs: Any) -> None:
        """Call emit if available, otherwise no-op."""
        if self.emit:
            self.emit(*args, **kwargs)
