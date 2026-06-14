"""
Family Office V3 — Data Models & Contracts.

Pydantic models for the Manager-Worker agentic debate architecture.
The CIO (Chief Investment Officer) controls a dynamic debate loop
where PMs submit arguments, workers fetch data on demand, and the
CIO decides when sufficient evidence exists to render a verdict.

Backward-compatible: FamilyOfficeResult inherits from DebateResult
so downstream consumers (trading phase, reports, post-cycle hooks)
work unchanged.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ManagerRole(str, Enum):
    """The 8 persistent Manager roles in the Family Office + 7 Civilization Council archetypes."""
    CIO = "cio"
    FUNDAMENTAL_PM = "fundamental_pm"
    GROWTH_PM = "growth_pm"
    MACRO_PM = "macro_pm"
    RISK_MANAGER = "risk_manager"
    CROSS_EXAMINER = "cross_examiner"
    MEMORY_PM = "memory_pm"
    WORKER_ORCHESTRATOR = "worker_orchestrator"

    IMHOTEP = "imhotep"
    PYTHAGORAS = "pythagoras"
    ARCHIMEDES = "archimedes"
    CAESAR = "caesar"
    AL_KHWARIZMI = "al_khwarizmi"
    BRAHMAGUPTA = "brahmagupta"
    NEWTON_LEIBNIZ = "newton_leibniz"


class WorkerType(str, Enum):
    """Specialized worker analyst types."""
    QUANT = "worker_quant"
    FUNDAMENTAL = "worker_fundamental"
    NEWS = "worker_news"
    INSIDER = "worker_insider"


class DataRequest(BaseModel):
    """A structured request from a PM to the Worker Orchestrator.

    PMs do NOT call tools directly — they submit DataRequests describing
    what data they need. The Worker Orchestrator dispatches the
    appropriate worker analyst.
    """
    requesting_manager: ManagerRole
    worker_type: WorkerType
    description: str  # Natural language: "I need last 4 quarters of revenue and margins"
    priority: str = "normal"  # "critical" | "normal" | "optional"
    ticker: str = ""
    specific_metrics: List[str] = Field(default_factory=list)  # e.g., ["revenue", "margins", "fcf"]

    class Config:
        frozen = True


class WorkerResult(BaseModel):
    """Structured result from a worker analyst."""
    worker_type: WorkerType
    request_description: str
    data: str  # The raw data fetched by the worker
    source: str = ""  # Tool/API source name
    freshness: str = ""  # e.g., "realtime", "1h_old", "1d_old"
    success: bool = True
    error: str = ""
    tool_calls_made: List[str] = Field(default_factory=list)

    class Config:
        frozen = True


class ManagerArgument(BaseModel):
    """A single PM's submitted argument to the CIO.

    Each PM analyzes its domain-specific evidence and submits a structured
    argument. Claims MUST include inline citations [source:value].
    """
    role: ManagerRole
    claims: List[str] = Field(default_factory=list)
    confidence: int = 0  # 0-100
    conviction: str = ""  # WATCH | LOW | MODERATE | HIGH | EXTREME
    direction: str = "neutral"  # bull | bear | neutral
    key_argument: str = ""
    devils_advocate: str = ""  # Strongest argument AGAINST their own case
    data_requests: List[DataRequest] = Field(default_factory=list)  # What more data they need
    reasoning_approach: str = ""  # e.g., "first_principles", "analogical", "inductive"
    raw_response: str = ""  # Full LLM response for audit trail
    tokens_used: int = 0

    class Config:
        frozen = True


class CIODirectiveStatus(str, Enum):
    """What the CIO decides after reviewing arguments."""
    NEEDS_MORE_DATA = "needs_more_data"
    READY_FOR_VERDICT = "ready_for_verdict"
    ABSTAIN = "abstain"


class CIODirective(BaseModel):
    """The CIO's response after reviewing all PM arguments.

    If status is NEEDS_MORE_DATA, the data_requests list specifies
    what additional data the CIO wants before making a decision.
    If READY_FOR_VERDICT, the CIO has enough evidence.
    If ABSTAIN, the data quality is too poor to decide.
    """
    status: CIODirectiveStatus
    rationale: str = ""
    data_requests: List[DataRequest] = Field(default_factory=list)
    directed_managers: List[ManagerRole] = Field(default_factory=list)  # Which PMs should re-analyze
    round_number: int = 0

    class Config:
        frozen = True


class DebateRound(BaseModel):
    """One full round of the dynamic CIO-driven debate loop.

    Captures PM arguments, cross-exam findings, worker results,
    and the CIO's directive for that round.
    """
    round_number: int
    pm_arguments: List[ManagerArgument] = Field(default_factory=list)
    cross_exam_findings: str = ""
    worker_results: List[WorkerResult] = Field(default_factory=list)
    cio_directive: Optional[CIODirective] = None
    tokens_used: int = 0
    elapsed_ms: int = 0

    class Config:
        frozen = True


class FamilyOfficeVerdict(BaseModel):
    """The CIO's final verdict after the debate concludes."""
    action: str = "HOLD"  # BUY | SELL | HOLD
    confidence: int = 0
    winning_side: str = "split"  # "bull" | "bear" | "split"
    key_deciding_factor: str = ""
    rejected_claim_impact: str = ""
    rationale: str = ""
    conviction: str = ""  # WATCH | LOW | MODERATE | HIGH | EXTREME
    original_thesis_status: str = "NOT_HELD"
    original_thesis_explanation: str = ""
    dissenting_managers: List[str] = Field(default_factory=list)
    tokens_used: int = 0


# ── Backward-compatible result ────────────────────────────────────────
# FamilyOfficeResult can be converted to DebateResult for downstream
# consumers that expect the V2 shape.

class FamilyOfficeResult(BaseModel):
    """Full output of the V3 Family Office debate pipeline.

    Contains all debate rounds, the CIO's final verdict, and
    per-manager outcomes. Can be converted to a DebateResult for
    backward compatibility with the V2 pipeline.
    """
    ticker: str = ""
    debate_rounds: List[DebateRound] = Field(default_factory=list)
    verdict: Optional[FamilyOfficeVerdict] = None
    memory_context_injected: str = ""  # What the Memory PM provided
    integrity_status: str = "HIGH"
    total_tokens: int = 0
    total_rounds: int = 0
    max_rounds_reached: bool = False
    manager_outcomes: dict = Field(default_factory=dict)

    def to_debate_result(self) -> "DebateResult":
        """Convert to V2 DebateResult for backward compatibility.

        Maps the richer V3 structure into the flatter V2 shape so
        downstream consumers (trading phase, reports, post-cycle
        hooks) work unchanged.
        """
        from app.cognition.contracts.debate import DebateResult

        verdict = self.verdict or FamilyOfficeVerdict()

        # Collect all claims across rounds, categorizing by bull/bear bias
        bull_claims: list[dict] = []
        bear_claims: list[dict] = []
        all_cross_findings: list[str] = []

        for rnd in self.debate_rounds:
            for arg in rnd.pm_arguments:
                # Check explicit direction, fallback to role-based logic if not set or neutral
                side = arg.direction
                if side not in ("bull", "bear"):
                    side = "bear" if arg.role in (ManagerRole.RISK_MANAGER, ManagerRole.CAESAR) else "bull"
                for claim in arg.claims:
                    entry = {
                        "claim": claim,
                        "turn": rnd.round_number,
                        "survived_rebuttal": rnd.round_number > 1,
                    }
                    if side == "bear":
                        bear_claims.append(entry)
                    else:
                        bull_claims.append(entry)

            if rnd.cross_exam_findings:
                all_cross_findings.append(
                    f"[Round {rnd.round_number}] {rnd.cross_exam_findings}"
                )

        # Build transcript from all rounds
        transcript_parts = []
        for rnd in self.debate_rounds:
            transcript_parts.append(f"=== ROUND {rnd.round_number} ===")
            for arg in rnd.pm_arguments:
                transcript_parts.append(
                    f"### {arg.role.value.upper()}\n"
                    + "\n".join(f"- {c}" for c in arg.claims)
                )
            if rnd.cross_exam_findings:
                transcript_parts.append(
                    f"### CROSS-EXAM\n{rnd.cross_exam_findings}"
                )
            if rnd.cio_directive:
                transcript_parts.append(
                    f"### CIO DIRECTIVE: {rnd.cio_directive.status.value}\n"
                    f"{rnd.cio_directive.rationale}"
                )

        return DebateResult(
            bull_claims=bull_claims,
            bear_claims=bear_claims,
            verified_bull_claims=bull_claims,  # V3 cross-exam verifies inline
            verified_bear_claims=bear_claims,
            unverified_claims=[],
            cross_exam_findings=" | ".join(all_cross_findings),
            judge_action=verdict.action,
            judge_confidence=verdict.confidence,
            judge_rationale=verdict.rationale,
            winning_side=verdict.winning_side,
            key_deciding_factor=verdict.key_deciding_factor,
            rejected_claim_impact=verdict.rejected_claim_impact,
            integrity_status=self.integrity_status,
            transcript="\n\n".join(transcript_parts),
            total_tokens=self.total_tokens,
            persona_outcomes=self.manager_outcomes,
            original_thesis_status=verdict.original_thesis_status,
            original_thesis_explanation=verdict.original_thesis_explanation,
        )
