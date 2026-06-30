"""
SharedDesk — The central state machine for V3 pipeline.

One desk per (cycle_id, ticker). Agents read and append typed artifacts.
Orchestrator advances the phase. Persisted to Postgres.

Phase transitions: INIT → RESEARCH_DONE → DEBATE_DONE → PM_DONE | ABORTED
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class DeskPhase(str, Enum):
    """Strict phase progression for the V3 pipeline."""
    INIT = "INIT"
    RESEARCH_DONE = "RESEARCH_DONE"
    DEBATE_DONE = "DEBATE_DONE"
    PM_DONE = "PM_DONE"
    ABORTED = "ABORTED"


class PhaseOutcome(str, Enum):
    """Outcome classification for each pipeline phase."""
    SUCCESS = "SUCCESS"
    DATA_GAP = "DATA_GAP"
    TOOL_OUTAGE = "TOOL_OUTAGE"
    AGENT_ERROR = "AGENT_ERROR"
    TIMED_OUT = "TIMED_OUT"


# Valid phase transitions — enforced by SharedDesk.advance_phase()
_VALID_TRANSITIONS: dict[DeskPhase, set[DeskPhase]] = {
    DeskPhase.INIT: {DeskPhase.RESEARCH_DONE, DeskPhase.ABORTED},
    DeskPhase.RESEARCH_DONE: {DeskPhase.DEBATE_DONE, DeskPhase.ABORTED},
    DeskPhase.DEBATE_DONE: {DeskPhase.PM_DONE, DeskPhase.ABORTED},
    DeskPhase.PM_DONE: set(),   # Terminal
    DeskPhase.ABORTED: set(),    # Terminal
}

# Artifact types that can be appended to the desk
_VALID_ARTIFACT_TYPES = frozenset({
    "desk_note",
    "fundamental_report",
    "quant_report",
    "bull_argument",
    "bear_rebuttal",
    "bull_defense",
    "debate_judge",
    "regime_classification",
    "final_decision",
    "trade_decision",
})

# Max compressed context size to prevent context snowball
_MAX_COMPRESSED_CONTEXT_CHARS = 8000


@dataclass
class SharedDesk:
    """Central state object for one ticker's V3 pipeline lifecycle.

    Agents read from and append typed artifacts to the desk.
    The orchestrator advances the phase after validating artifacts.
    """

    desk_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cycle_id: str = ""
    ticker: str = ""
    phase: DeskPhase = DeskPhase.INIT
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # ── Typed artifacts — populated by agents as they complete ──
    desk_note: dict | None = None           # Junior Analyst output
    fundamental_report: dict | None = None  # Fundamental Analyst output
    quant_report: dict | None = None        # Quant/Risk Analyst output
    bull_argument: dict | None = None       # Bull Agent output
    bear_rebuttal: dict | None = None       # Bear Agent output
    bull_defense: dict | None = None        # Bull Agent final defense
    debate_judge: dict | None = None        # Debate Judge output
    regime_classification: dict | None = None  # Market Regime Engine output
    final_decision: dict | None = None      # Board of Directors output
    trade_decision: dict | None = None      # Decision Synthesizer output (Layer 5)

    # ── Phase outcome tracking ──
    phase_outcomes: dict[str, str] = field(default_factory=dict)

    # ── Cycle metadata injected in Layer 1 ──
    cycle_metadata: dict[str, Any] = field(default_factory=dict)

    # ── Telemetry ──
    agent_telemetry: list[dict[str, Any]] = field(default_factory=list)

    def append_artifact(self, artifact_type: str, artifact: dict) -> None:
        """Append a typed artifact to the desk.

        Args:
            artifact_type: One of the valid artifact types (e.g. 'desk_note').
            artifact: The artifact dict to append. Must have a 'summary' key.

        Raises:
            ValueError: If artifact_type is invalid.
        """
        if artifact_type not in _VALID_ARTIFACT_TYPES:
            raise ValueError(
                f"Invalid artifact_type: {artifact_type}. "
                f"Valid: {sorted(_VALID_ARTIFACT_TYPES)}"
            )

        # Stamp metadata
        artifact["_appended_at"] = datetime.now(timezone.utc).isoformat()
        artifact["_artifact_type"] = artifact_type

        setattr(self, artifact_type, artifact)
        _size = len(json.dumps(artifact, default=str))
        logger.info(
            "[SharedDesk] %s/%s: Appended %s (%d bytes)",
            self.cycle_id[:12] if self.cycle_id else "?",
            self.ticker,
            artifact_type,
            _size,
        )

    def advance_phase(
        self,
        new_phase: DeskPhase,
        outcome: PhaseOutcome = PhaseOutcome.SUCCESS,
    ) -> None:
        """Advance to the next phase with strict transition validation.

        Args:
            new_phase: The target phase.
            outcome: The outcome of the current phase.

        Raises:
            ValueError: If the transition is invalid.
        """
        allowed = _VALID_TRANSITIONS.get(self.phase, set())
        if new_phase not in allowed:
            raise ValueError(
                f"Invalid transition: {self.phase.value} → {new_phase.value}. "
                f"Valid targets: {sorted(p.value for p in allowed)}"
            )

        old_phase = self.phase
        self.phase = new_phase
        self.phase_outcomes[old_phase.value] = outcome.value
        logger.info(
            "[SharedDesk] %s/%s: Phase %s → %s (outcome: %s)",
            self.cycle_id[:12] if self.cycle_id else "?",
            self.ticker,
            old_phase.value,
            new_phase.value,
            outcome.value,
        )

    def has_artifact(self, artifact_type: str) -> bool:
        """Check if a specific artifact has been appended."""
        return getattr(self, artifact_type, None) is not None

    def get_research_artifacts(self) -> dict[str, dict]:
        """Return all research layer artifacts (non-None)."""
        result = {}
        for name in ("desk_note", "fundamental_report", "quant_report"):
            val = getattr(self, name, None)
            if val is not None:
                result[name] = val
        return result

    def get_debate_artifacts(self) -> dict[str, dict]:
        """Return all debate layer artifacts (non-None)."""
        result = {}
        for name in ("bull_argument", "bear_rebuttal", "bull_defense", "debate_judge"):
            val = getattr(self, name, None)
            if val is not None:
                result[name] = val
        return result

    def get_compressed_context(self, include_debate: bool = False) -> str:
        """Build a compressed narrative for downstream agents.

        Returns only the summary fields from artifacts — drops raw tool JSON,
        intermediate scratch, etc. This prevents context snowball.

        Args:
            include_debate: If True, include debate artifacts too.

        Returns:
            A clean narrative string ≤ _MAX_COMPRESSED_CONTEXT_CHARS.
        """
        sections: list[str] = []

        # Research artifacts
        if self.desk_note:
            summary = self.desk_note.get("summary", "")
            key_findings = self.desk_note.get("key_findings", [])
            data_gaps = self.desk_note.get("data_gaps", [])
            text = f"## Junior Analyst Notes\n{summary}"
            if key_findings:
                text += "\n**Key Findings:**\n" + "\n".join(
                    f"- {f}" for f in key_findings[:5]
                )
            if data_gaps:
                text += "\n**Data Gaps:**\n" + "\n".join(
                    f"- DataGap: {g}" for g in data_gaps[:3]
                )
            sections.append(text)

        if self.fundamental_report:
            summary = self.fundamental_report.get("summary", "")
            direction = self.fundamental_report.get("thesis_direction", "?")
            conf = self.fundamental_report.get("confidence", 0)
            data_gaps = self.fundamental_report.get("data_gaps", [])
            text = (
                f"## Fundamental Analysis\n"
                f"**Direction: {direction} @ {conf}% confidence**\n{summary}"
            )
            if data_gaps:
                text += "\n**Data Gaps:**\n" + "\n".join(
                    f"- DataGap: {g}" for g in data_gaps[:3]
                )
            sections.append(text)

        if self.quant_report:
            summary = self.quant_report.get("summary", "")
            direction = self.quant_report.get("thesis_direction", "?")
            conf = self.quant_report.get("confidence", 0)
            risk = self.quant_report.get("risk_metrics", {})
            text = (
                f"## Quantitative / Risk Analysis\n"
                f"**Direction: {direction} @ {conf}% confidence**\n{summary}"
            )
            if risk:
                metrics = ", ".join(
                    f"{k}={v}" for k, v in risk.items() if v is not None
                )
                if metrics:
                    text += f"\n**Key Metrics:** {metrics}"
            sections.append(text)

        # Debate artifacts (only if requested)
        if include_debate:
            if self.bull_argument:
                summary = self.bull_argument.get("summary", "")
                conf = self.bull_argument.get("confidence", 0)
                sections.append(
                    f"## Bull Thesis (confidence: {conf}%)\n{summary}"
                )

            if self.bear_rebuttal:
                summary = self.bear_rebuttal.get("summary", "")
                conf = self.bear_rebuttal.get("confidence", 0)
                sections.append(
                    f"## Bear Rebuttal (confidence: {conf}%)\n{summary}"
                )

            if self.bull_defense:
                summary = self.bull_defense.get("summary", "")
                sections.append(f"## Bull Final Defense\n{summary}")

            if self.debate_judge:
                summary = self.debate_judge.get("summary", "")
                winner = self.debate_judge.get("winner", "")
                conf = self.debate_judge.get("final_confidence", 0)
                sections.append(f"## Debate Judge Verdict (Winner: {winner} @ {conf}% confidence)\n{summary}")

        # Regime
        if self.regime_classification:
            regime = self.regime_classification.get("regime", "?")
            conf = self.regime_classification.get("confidence", 0)
            rationale = self.regime_classification.get("rationale", "")
            sections.append(
                f"## Market Regime: {regime} ({conf}% confidence)\n{rationale}"
            )

        combined = (
            "\n\n---\n\n".join(sections)
            if sections
            else "No artifacts on desk yet."
        )

        # Truncate to prevent context snowball
        if len(combined) > _MAX_COMPRESSED_CONTEXT_CHARS:
            combined = (
                combined[: _MAX_COMPRESSED_CONTEXT_CHARS - 100]
                + "\n\n[... TRUNCATED — full artifacts available on SharedDesk ...]"
            )

        return combined

    def record_agent_telemetry(self, entry: dict[str, Any]) -> None:
        """Record a telemetry entry for an agent run."""
        entry["_recorded_at"] = datetime.now(timezone.utc).isoformat()
        self.agent_telemetry.append(entry)

    # ── Serialization ──

    def to_dict(self) -> dict[str, Any]:
        """Serialize for DB persistence."""
        return {
            "desk_id": self.desk_id,
            "cycle_id": self.cycle_id,
            "ticker": self.ticker,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "desk_note": self.desk_note,
            "fundamental_report": self.fundamental_report,
            "quant_report": self.quant_report,
            "bull_argument": self.bull_argument,
            "bear_rebuttal": self.bear_rebuttal,
            "bull_defense": self.bull_defense,
            "debate_judge": self.debate_judge,
            "regime_classification": self.regime_classification,
            "final_decision": self.final_decision,
            "trade_decision": self.trade_decision,
            "phase_outcomes": self.phase_outcomes,
            "cycle_metadata": self.cycle_metadata,
            "agent_telemetry": self.agent_telemetry,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SharedDesk:
        """Deserialize from DB."""
        desk = cls()
        desk.desk_id = data.get("desk_id", str(uuid.uuid4()))
        desk.cycle_id = data.get("cycle_id", "")
        desk.ticker = data.get("ticker", "")
        desk.phase = DeskPhase(data.get("phase", "INIT"))
        desk.created_at = data.get("created_at", "")
        desk.desk_note = data.get("desk_note")
        desk.fundamental_report = data.get("fundamental_report")
        desk.quant_report = data.get("quant_report")
        desk.bull_argument = data.get("bull_argument")
        desk.bear_rebuttal = data.get("bear_rebuttal")
        desk.bull_defense = data.get("bull_defense")
        desk.debate_judge = data.get("debate_judge")
        desk.regime_classification = data.get("regime_classification")
        desk.final_decision = data.get("final_decision")
        desk.trade_decision = data.get("trade_decision")
        desk.phase_outcomes = data.get("phase_outcomes", {})
        desk.cycle_metadata = data.get("cycle_metadata", {})
        desk.agent_telemetry = data.get("agent_telemetry", [])
        return desk
