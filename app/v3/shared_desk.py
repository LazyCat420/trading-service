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
    "tournament_result",
    "delta_report",
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
    tournament_result: dict | None = None    # Tournament Debate output (Layer 3 alt)
    delta_report: dict | None = None        # Delta Analyst output (fast re-look tier)

    # ── Agent data tags — free-form labels harvested from artifacts ──
    # artifact_type -> ["#catalyst", "#risk", ...]. Lets agents mark data
    # points for later reference; re-surfaced in compressed context and the
    # next cycle's handoff brief.
    artifact_tags: dict[str, list[str]] = field(default_factory=dict)

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

        # Harvest optional free-form tags the agent put in its JSON (the
        # output directive advertises this). Normalized to '#lowercase'.
        raw_tags = artifact.get("tags")
        if isinstance(raw_tags, list):
            existing = self.artifact_tags.setdefault(artifact_type, [])
            for t in raw_tags[:10]:
                t = str(t).strip().lower().replace(" ", "_")
                if not t:
                    continue
                if not t.startswith("#"):
                    t = f"#{t}"
                t = t[:40]
                if t not in existing:
                    existing.append(t)
            if not existing:
                self.artifact_tags.pop(artifact_type, None)

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

    def get_handoff_brief(self) -> str:
        """Compact structured brief of this desk for the NEXT cycle's context.

        The Manila-Envelope injection used to ship the full compressed
        narrative (up to 8,000 chars) into every downstream agent's prompt.
        Continuity only needs the decision and the headline findings — keep
        it to a few hundred chars (plan 4.4).
        """
        decision = self.trade_decision or self.final_decision or {}
        parts: list[str] = []

        action = decision.get("action")
        if action:
            parts.append(
                f"Previous decision: {action} @ {decision.get('confidence', '?')}% confidence"
            )
        regime = (self.regime_classification or {}).get("regime") or decision.get("regime")
        if regime:
            parts.append(f"Regime then: {regime}")

        key_findings = (self.desk_note or {}).get("key_findings") or []
        for finding in key_findings[:3]:
            parts.append(f"- {str(finding)[:160]}")

        reasoning = decision.get("reasoning", "")
        if reasoning:
            parts.append(f"Rationale: {reasoning[:200]}")

        all_tags = self.get_all_tags()
        if all_tags:
            parts.append("Tags flagged last cycle: " + ", ".join(all_tags[:12]))

        if not parts:
            return ""
        return "\n".join(parts)[:800]

    def get_all_tags(self) -> list[str]:
        """Deduped union of all agent-applied artifact tags, insertion order."""
        seen: list[str] = []
        for tags in self.artifact_tags.values():
            for t in tags:
                if t not in seen:
                    seen.append(t)
        return seen

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
            open_questions = self.quant_report.get("sub_analyses_requested") or []
            if open_questions:
                text += "\n**Open questions the Quant could not resolve:**\n" + "\n".join(
                    f"- {q}" for q in open_questions[:5]
                )
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

            tournament = getattr(self, "tournament_result", None)
            if tournament:
                action = tournament.get("action", "?")
                conf = tournament.get("confidence", 0)
                side = tournament.get("winning_side", "split")
                veto = " [JURY VETO]" if tournament.get("vetoed") else ""
                text = (
                    f"## Tournament Debate Verdict{veto}\n"
                    f"**{action} @ {conf}% confidence (winner: {side})**\n"
                    f"{tournament.get('summary', '')}"
                )
                # Debate nuance for the board: each side's attack points are
                # the tournament's equivalent of the classic judge's
                # weaknesses_of_winner / strongest_point_of_loser. A confident
                # verdict whose loser landed real blows deserves tighter stops.
                h2h = tournament.get("h2h") or {}
                for side_key, label in (("thesis_a", "Thesis A"), ("thesis_b", "Thesis B")):
                    thesis = h2h.get(side_key) or {}
                    attacks = thesis.get("attack_points") or []
                    if attacks:
                        persona = thesis.get("persona", label)
                        text += f"\n**{label} ({persona}) attack points:**\n" + "\n".join(
                            f"- {str(a)[:200]}" for a in attacks[:3]
                        )
                    # The board/synthesizer turn these into stop placement and
                    # dynamic re-analysis triggers — an explicit break condition
                    # beats a generic ATR stop.
                    invalidation = thesis.get("invalidation_condition")
                    if invalidation:
                        text += f"\n**{label} invalidation:** {str(invalidation)[:200]}"
                    window = thesis.get("catalyst_window")
                    if window:
                        text += f"\n**{label} catalyst window:** {str(window)[:150]}"
                jury_results = (tournament.get("jury_verdict") or {}).get("jury_results") or {}
                juror_lines = []
                for juror, verdict in list(jury_results.items())[:3]:
                    if isinstance(verdict, dict) and verdict.get("reasoning"):
                        flag = " [VETO]" if verdict.get("veto") else ""
                        juror_lines.append(
                            f"- {juror}{flag}: {str(verdict['reasoning'])[:200]}"
                        )
                if juror_lines:
                    text += "\n**Juror reasoning:**\n" + "\n".join(juror_lines)
                sections.append(text)

            # Skip the debate_judge artifact when it is just a copy of the
            # tournament verdict already rendered above.
            if self.debate_judge and not (tournament and self.debate_judge.get("source") == "tournament_debate"):
                summary = self.debate_judge.get("summary", "")
                # Tournament mode writes winning_side/confidence; the classic
                # debate judge wrote winner/final_confidence — accept both.
                winner = self.debate_judge.get("winning_side") or self.debate_judge.get("winner", "")
                conf = self.debate_judge.get("confidence", self.debate_judge.get("final_confidence", 0))
                text = f"## Debate Judge Verdict (Winner: {winner} @ {conf}% confidence)\n{summary}"
                weaknesses = self.debate_judge.get("weaknesses_of_winner") or []
                if weaknesses:
                    text += "\n**Winner's weak points:**\n" + "\n".join(
                        f"- {w}" for w in weaknesses[:3]
                    )
                loser_best = self.debate_judge.get("strongest_point_of_loser", "")
                if loser_best:
                    text += f"\n**Loser's best point:** {loser_best}"
                sections.append(text)

        # Regime
        if self.regime_classification:
            regime = self.regime_classification.get("regime", "?")
            conf = self.regime_classification.get("confidence", 0)
            rationale = self.regime_classification.get("rationale", "")
            text = f"## Market Regime: {regime} ({conf}% confidence)\n{rationale}"
            factors = self.regime_classification.get("factors") or {}
            if isinstance(factors, dict) and factors:
                rendered = ", ".join(
                    f"{k}={v}" for k, v in factors.items() if isinstance(v, (int, float))
                )
                if rendered:
                    text += f"\n**Regime Factors (0-1):** {rendered}"
            tags = self.regime_classification.get("market_context_tags") or []
            if tags:
                text += "\n**Market Context Tags:** " + ", ".join(str(t) for t in tags[:8])
            directive = self.regime_classification.get("board_directive", "")
            if directive:
                text += f"\n**Regime Engine's Directive to the Board:** {directive}"
            sections.append(text)

        # Agent-applied data tags (grouped by the artifact that raised them)
        if self.artifact_tags:
            tag_lines = [
                f"- {atype}: {', '.join(tags[:8])}"
                for atype, tags in self.artifact_tags.items() if tags
            ]
            if tag_lines:
                sections.append("## Desk Tags\n" + "\n".join(tag_lines))

        # Board of Directors
        if self.final_decision:
            action = self.final_decision.get("action", "?")
            conf = self.final_decision.get("confidence", 0)
            reasoning = self.final_decision.get("reasoning", "")
            sections.append(
                f"## Board of Directors Verdict\n**Action: {action} @ {conf}% confidence**\n{reasoning}"
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
            "tournament_result": self.tournament_result,
            "delta_report": self.delta_report,
            "artifact_tags": self.artifact_tags,
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
        desk.tournament_result = data.get("tournament_result")
        desk.delta_report = data.get("delta_report")
        desk.artifact_tags = data.get("artifact_tags") or {}
        desk.phase_outcomes = data.get("phase_outcomes", {})
        desk.cycle_metadata = data.get("cycle_metadata", {})
        desk.agent_telemetry = data.get("agent_telemetry", [])
        return desk
