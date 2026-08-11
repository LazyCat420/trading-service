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

TOURNAMENT_MODE_ACTIVE = "active"
TOURNAMENT_MODE_SHADOW = "shadow"


#: Severity prefixes a producer may stamp on a data_gaps entry. Introduced
#: 2026-07-29 because every gap read identically to the Board and the effect was
#: binary rather than graded: desks with ZERO gaps cleared the confidence floor
#: 73% of the time, desks with ONE OR MORE cleared it 4% (Fisher p=4.2e-09,
#: OR=62) — while MEAN confidence was identical either way (61.0 vs 60.9). One
#: routine gap was worth as much as a missing price history.
_GAP_SEVERITIES = ("BLOCKING", "MATERIAL", "MINOR")

#: Gaps written by an analyst LLM carry no tag. They are the ordinary kind — a
#: missing 5-year margin trend, an unquantified regulatory risk — so they render
#: as MINOR rather than inheriting the weight of a BLOCKING one.
_DEFAULT_GAP_SEVERITY = "MINOR"


def render_data_gap(gap: Any) -> str:
    """Render one data_gaps entry with an explicit severity for the Board.

    Producers may prefix an entry with ``[BLOCKING]`` / ``[MATERIAL]`` /
    ``[MINOR]``; anything untagged is MINOR. The tag is surfaced rather than
    stripped so the Board can weigh gaps instead of counting them — the prompt
    rubric tells it that a MINOR gap in a figure the thesis does not rest on is
    routine and should not by itself move a decision below the floor.
    """
    text = str(gap).strip()
    for sev in _GAP_SEVERITIES:
        prefix = f"[{sev}]"
        if text.upper().startswith(prefix):
            return f"{prefix} {text[len(prefix):].strip()}"
    return f"[{_DEFAULT_GAP_SEVERITY}] {text}"


def render_stale_conclusion(artifact: dict | None) -> str:
    """Render the code-written stale-conclusion flag, or "" if the call stands.

    Set by the reconcile passes via
    `app/quant/technical_baseline.py:mark_conclusion_stale`. Rendered here or it
    reaches nobody: the Board and the debate read the desk through this
    compressed view, not the raw artifact, so a flag that is computed and never
    rendered is work nothing downstream can see.

    Mirrors the wording of the positioning `stance_is_stale` note below —
    weigh the corrected numbers, not the label built on the old ones.
    """
    if not isinstance(artifact, dict) or not artifact.get("_conclusion_is_stale"):
        return ""
    fields = artifact.get("_conclusion_stale_fields") or []
    reason = artifact.get("_conclusion_stale_reason") or ""
    which = ", ".join(str(f) for f in fields) or "the conclusion"
    return (
        f"\n> ⚠ STALE CONCLUSION — {which} was reasoned from numbers that have "
        f"since been corrected; weigh the verified metrics above, not the call. "
        f"({reason})"
    )


def tournament_debate_mode() -> str:
    """Resolve TOURNAMENT_DEBATE_MODE to "active" or "shadow".

    Fail-open: the shadow branch REMOVES evidence from the Board, so any
    failure to read the parameter must land on today's behaviour rather than
    silently blinding the decision. Only an explicit, honest 1 flips it.
    """
    try:
        from app.services.parameter_store import get_param
        return TOURNAMENT_MODE_SHADOW if int(get_param("TOURNAMENT_DEBATE_MODE")) == 1 else TOURNAMENT_MODE_ACTIVE
    except Exception as e:  # noqa: BLE001 — a param miss must never blind the Board
        logger.warning("[V3] TOURNAMENT_DEBATE_MODE lookup failed (%s) — staying active", e)
        return TOURNAMENT_MODE_ACTIVE


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


class DecisionProvenance(str, Enum):
    """How a decision artifact came to hold the action it holds.

    The failure mode this exists to kill: a degraded board produced no
    `final_decision`, the pipeline fell through to a hardcoded
    `{"action": "HOLD", "confidence": 0}`, and the desk recorded something
    indistinguishable from a confident no-signal HOLD. Ten such desks were
    found on 2026-07-25, all HOLD — not because the board is biased, but
    because the degrade path and the HOLD default share one cause.

    Deferred item 8.2 fixed exactly this for board *timeouts* in 2026-07-15
    and did not generalize. Making provenance a REQUIRED, enumerated field
    rather than a convention is the generalization: a new fallback path
    cannot silently reintroduce the bug, because it cannot produce a decision
    artifact without saying where the action came from.

    Only BOARD_REASONED means "an agent actually decided this". Everything
    else is excluded from accuracy scoring by default (`--reasoned-only`).
    """
    BOARD_REASONED = "board_reasoned"                    # the board genuinely decided
    BOARD_DEGRADED_FALLBACK = "board_degraded_fallback"  # board failed; default HOLD stood in
    NO_TRADE_GATE_SKIP = "no_trade_gate_skip"            # unheld + unanimously bearish → debate skipped
    COERCED_UNSHORTABLE = "coerced_unshortable"          # SELL on an unheld ticker rewritten to HOLD
    TIMEOUT_ABORT = "timeout_abort"                      # phase timed out; desk aborted
    # 2026-07-25: the two triage writers (Triage Gate glance-skip and JA
    # triage SKIP) wrote a hardcoded HOLD@0 with no provenance and were
    # therefore stamped board_reasoned by the old permissive default — so the
    # scorecard counted them as real board opinions, though NO agent ran. Kept
    # distinct from NO_TRADE_GATE_SKIP deliberately: that gate fires AFTER the
    # research tier on an unheld + unanimously bearish desk, whereas this is a
    # pre-agent age/news-count heuristic. Different cause, different fix
    # (TRIAGE_GLANCE_HOURS vs. the no-trade gate) — collapsing them would make
    # "why did we not decide?" unanswerable from the field that exists to
    # answer it. One member covers both writers because `persona_used` already
    # separates them.
    TRIAGE_SKIP = "triage_skip"                          # triage heuristic skipped it; no agent ran
    # The honest answer when nothing claimed the decision. This is the
    # append_artifact default: an unstamped decision artifact is NOT evidence
    # an agent decided, and saying so out loud is what stops the next fallback
    # path from laundering itself into the accuracy numbers.
    UNATTRIBUTED = "unattributed"                        # nobody claimed it; not an agent decision

    @classmethod
    def scoreable(cls) -> frozenset[str]:
        """Provenances whose action reflects a real decision, so accuracy
        measured over them means something."""
        return frozenset({cls.BOARD_REASONED.value})


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
    "valuation_report",
    "bull_argument",
    "bear_rebuttal",
    "bull_defense",
    "debate_judge",
    "regime_classification",
    "final_decision",
    "trade_decision",
    "tournament_result",
    "delta_report",
    # The triage override writes this when research_degraded() fires. It was
    # never in this set, so append_artifact raised ValueError, the whiteboard
    # swallowed the raise, and EVERY analyst dispatch below that line was dead
    # code — the desk stayed at INIT and the ticker was lost with a decision
    # row that claimed a full pipeline ran. 22 attempts 2026-07-28..08-10, 0
    # completions. See tests/unit/test_desk_phase_transition.py.
    "degradation_note",
})

# Artifacts that carry a tradeable action, and therefore must always declare
# where that action came from. See DecisionProvenance.
_DECISION_ARTIFACT_TYPES = frozenset({"final_decision", "trade_decision"})

_VALID_PROVENANCE = frozenset(p.value for p in DecisionProvenance)

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
    valuation_report: dict | None = None    # Valuation Analyst output
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

        # Every decision artifact says where its action came from. Stamped
        # HERE rather than at the ~6 call sites so a future fallback path
        # cannot produce an unmarked decision by forgetting to set it.
        # A caller that already set provenance (a degrade/coercion path) wins.
        #
        # 2026-07-25: this default used to be BOARD_REASONED — fail-OPEN on
        # the single field whose whole purpose is to stop laundering. Any path
        # that forgot to stamp was automatically credited with "an agent
        # decided this", which is exactly how two hardcoded HOLD@0 triage
        # writers were scored as real board opinions. The default is now
        # UNATTRIBUTED: absence of a claim is not a claim. board_reasoned is
        # asserted only where an agent is KNOWN to have produced the artifact
        # (agent_runner's append call site, and the delta tier which bypasses
        # it), so a new fallback path can no longer inherit credibility by
        # omission.
        if artifact_type in _DECISION_ARTIFACT_TYPES:
            existing = artifact.get("decision_provenance")
            if existing not in _VALID_PROVENANCE:
                if existing is not None:
                    logger.warning(
                        "[SharedDesk] %s/%s: unknown decision_provenance %r on %s "
                        "— recording as %s",
                        self.cycle_id, self.ticker, existing, artifact_type,
                        DecisionProvenance.UNATTRIBUTED.value,
                    )
                # A coercion may have run before the artifact reached the desk
                # (validators run in agent_runner), so honour its marker.
                artifact["decision_provenance"] = (
                    DecisionProvenance.COERCED_UNSHORTABLE.value
                    if artifact.get("_coerced_from")
                    else DecisionProvenance.UNATTRIBUTED.value
                )

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
        # `outcome` grades the phase being LEFT, so a terminal phase (PM_DONE,
        # ABORTED) never got an entry of its own — 852/852 desks carried a
        # `phase` absent from `phase_outcomes`, which made "did the PM stage
        # actually run?" unanswerable from the desk. Record reaching a terminal
        # phase as an event in its own right.
        if not _VALID_TRANSITIONS.get(new_phase):
            self.phase_outcomes.setdefault(new_phase.value, "REACHED")
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
        for name in ("desk_note", "fundamental_report", "quant_report",
                     "valuation_report"):
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
            A clean narrative string capped at ~_MAX_COMPRESSED_CONTEXT_CHARS
            (the 500-char research floor can push it ~60 chars past the cap
            when the verdict block is near its own maximum).
        """
        sections: list[str] = []
        # Verdict-bearing sections (tournament/judge verdict, Board verdict) are
        # collected separately and appended AFTER truncation: the old layout
        # rendered them last and tail-truncated the whole string, so the Board
        # verdict was cut from the deciding agent's context on 133 of 134
        # decisions 07-28..08-04 — the synthesizer ("Baseline = the Board's
        # verdict", decision_agent.py) decided blind and on 08-04 hallucinated
        # the board's stance (ET: board BUY@63 reported as an aligned HOLD).
        # Research prose absorbs the cut; verdicts always render.
        verdict_sections: list[str] = []

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
                    f"- DataGap: {render_data_gap(g)}" for g in data_gaps[:3]
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
                f"{render_stale_conclusion(self.fundamental_report)}"
            )
            # Rendered for the same reason the quant's risk_metrics are: this
            # is what carries the fundamental side as NUMBERS into the debate
            # and the Board. Without it the desk offered "fundamentals remain
            # robust" against "RSI 78.0, Stochastic 98.9", and the measured
            # consequence was that overrides leaned on oscillators (+27.1pp
            # stochastic) and away from fundamentals (-21.2pp eps).
            # Positioning: rendered or it reaches nobody. The whole point of
            # the required field is that the evidence travels past the desk
            # that read it.
            pos = self.fundamental_report.get("positioning_read") or {}
            if isinstance(pos, dict) and pos:
                counts = ", ".join(
                    f"{k}={v}" for k, v in pos.items()
                    if isinstance(v, int) and not isinstance(v, bool)
                )
                stance = pos.get("stance")
                note = pos.get("note")
                if counts or stance:
                    stale = (" — STANCE IS STALE, it was reasoned from counts "
                             "that have since been corrected; weigh the counts, "
                             "not the label"
                             if pos.get("stance_is_stale") else "")
                    text += f"\n**Positioning ({stance or '?'}):** {counts}"
                    if note:
                        text += f" — {note}"
                    text += stale

            fmetrics = self.fundamental_report.get("metrics") or {}
            if fmetrics:
                rendered = ", ".join(
                    f"{k}={v}" for k, v in fmetrics.items() if v is not None
                )
                if rendered:
                    text += f"\n**Fundamental metrics (verified):** {rendered}"
            corrections = (
                self.fundamental_report.get("_model_reported_fundamentals")
                or self.fundamental_report.get("_unreconciled_fundamentals")
                or {}
            )
            if isinstance(corrections, dict) and corrections:
                applied = "_model_reported_fundamentals" in self.fundamental_report
                lines = []
                for field, was in corrections.items():
                    if isinstance(was, dict):
                        model_val, now = was.get("model"), was.get("verified")
                    else:
                        model_val, now = was, fmetrics.get(field)
                    if now is None:
                        continue
                    lines.append(
                        f"- {field}: text may say {model_val} — verified {now}"
                    )
                if lines:
                    text += (
                        "\n**Corrected figures — the prose above may quote the "
                        f"model's original"
                        f"{'' if applied else ' (NOT applied: stale snapshot)'}:**\n"
                        + "\n".join(lines)
                    )
            if data_gaps:
                text += "\n**Data Gaps:**\n" + "\n".join(
                    f"- DataGap: {render_data_gap(g)}" for g in data_gaps[:3]
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
                f"{render_stale_conclusion(self.quant_report)}"
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

        if self.valuation_report:
            # Rendered here or it reaches nobody. The Board and the debate read
            # the desk through this compressed view, not the raw artifact — a
            # valuation_report that is computed, reconciled and then never
            # rendered is work nothing downstream can see.
            verdict = self.valuation_report.get("verdict", "?")
            conf = self.valuation_report.get("confidence", 0)
            summary = self.valuation_report.get("summary", "")
            text = (
                f"## Valuation\n**Verdict: {verdict} @ {conf}% confidence**\n"
                f"{summary}"
                f"{render_stale_conclusion(self.valuation_report)}"
            )
            implied = self.valuation_report.get("price_implied_assumption")
            if implied:
                text += f"\n**Price implies:** {implied}"
            metrics = self.valuation_report.get("valuation_metrics") or {}
            if metrics:
                # ev_to_ebit is spelled out: an unlabelled multiple next to a
                # vendor EV/EBITDA elsewhere on the desk invites the Board to
                # compare two different quantities.
                rendered = ", ".join(
                    f"{k}={v}" for k, v in metrics.items() if v is not None
                )
                if rendered:
                    text += (f"\n**Multiples (EBIT-based, no D&A on file):** "
                             f"{rendered}")
            fair = self.valuation_report.get("fair_value_estimate")
            basis = self.valuation_report.get("fair_value_basis")
            if fair is not None:
                text += f"\n**Fair value:** {fair}"
                if basis:
                    text += f" ({basis})"
            changes = self.valuation_report.get("what_would_change_my_mind")
            if changes:
                text += f"\n**Would change the call:** {changes}"
            # The reconcile pass replaces bad numbers in `valuation_metrics`,
            # but the model also embeds them in `summary` and
            # `price_implied_assumption` — free text this module must not
            # rewrite, because judgment is the agent's job. In the 07-28 cycle
            # every figure that reached a final rationale was the model's
            # ORIGINAL, not the corrected one: PYPL quoted 1.1% implied growth
            # against a computed 0.77% and became a live BUY. Suppressing the
            # number in the field nobody reads downstream while leaving it in
            # the prose everybody does is not a guard. Naming both here lets
            # the Board see the disagreement without this module pretending to
            # an opinion about the thesis.
            corrections = (
                self.valuation_report.get("_model_reported_valuation")
                or self.valuation_report.get("_unreconciled_valuation")
                or {}
            )
            if isinstance(corrections, dict) and corrections:
                applied = "_model_reported_valuation" in self.valuation_report
                lines = []
                for field, was in corrections.items():
                    # _unreconciled_valuation nests {"model":..,"verified":..};
                    # _model_reported_valuation stores the scalar the model gave.
                    if isinstance(was, dict):
                        model_val = was.get("model")
                        now = was.get("verified")
                    else:
                        model_val = was
                        now = (metrics or {}).get(field)
                    if now is None:
                        continue
                    lines.append(f"- {field}: text may say {model_val} — computed {now}")
                if lines:
                    text += (
                        "\n**Corrected figures — the prose above may quote the "
                        f"model's original{'' if applied else ' (NOT applied: stale snapshot)'}:**\n"
                        + "\n".join(lines)
                    )
            gaps = self.valuation_report.get("data_gaps") or []
            if gaps:
                text += "\n**Data Gaps:**\n" + "\n".join(
                    f"- DataGap: {render_data_gap(g)}" for g in gaps[:3]
                )
            sections.append(text)

        # Debate artifacts (only if requested, and only in "active" mode).
        #
        # Shadow mode gates RENDERING, not execution: the debate still ran and
        # tournament_result is still on the desk, so the jury veto in
        # _apply_policy_gates (which reads desk.tournament_result directly, not
        # this string) fires identically either way. What shadow removes is the
        # winner's influence on the Board — measured at t = -0.17 against
        # realized P&L for 31% of pipeline spend. It gates the VERDICT sections
        # only: bull/bear/defense prose stays visible, because blinding the
        # BEAR to the bull it is rebutting is not part of the experiment (the
        # whiteboard summary already keeps bull/bear visible in shadow — this
        # matches that behavior).
        include_verdicts = include_debate
        if include_debate and tournament_debate_mode() == TOURNAMENT_MODE_SHADOW:
            include_verdicts = False

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
                # The two scalars only, deliberately. The defense artifact also
                # carries defense_points / concessions /
                # independent_risks_answered, and on cycle-v3-1786401874 those
                # ran to ~8k chars for META — roughly 2k tokens added to the
                # judge AND the board, the two agents whose non-sheddable
                # context was already logging 6.6-11.1k against a 2,048-token
                # embedder that cycle. They are also largely a restatement of
                # `summary`, which already opens "I concede…". The verdict and
                # the number are what summary does NOT state outright, they
                # cost ~15 tokens, and the bull and bear sections above have
                # carried their confidence all along.
                summary = self.bull_defense.get("summary", "")
                conf = self.bull_defense.get("final_confidence", 0)
                survives = self.bull_defense.get("thesis_survives")
                if isinstance(survives, str):
                    survives = survives.strip().lower() in ("true", "yes")
                verdict = (
                    "thesis SURVIVES" if survives
                    else "thesis DOES NOT survive" if survives is not None
                    else "verdict unstated"
                )
                sections.append(
                    f"## Bull Final Defense ({verdict}, confidence: {conf}%)\n{summary}"
                )

            tournament = getattr(self, "tournament_result", None)
            if tournament and include_verdicts:
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
                verdict_sections.append(text)

            # Skip the debate_judge artifact when it is just a copy of the
            # tournament verdict already rendered above.
            if include_verdicts and self.debate_judge and not (tournament and self.debate_judge.get("source") == "tournament_debate"):
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
                verdict_sections.append(text)

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

        # Board of Directors — FIRST among the verdicts: if verdict_text itself
        # ever overflows and tail-truncates, the tournament/judge prose absorbs
        # the cut, never the Board verdict the synthesizer baselines on.
        if self.final_decision:
            action = self.final_decision.get("action", "?")
            conf = self.final_decision.get("confidence", 0)
            reasoning = self.final_decision.get("reasoning", "")
            verdict_sections.insert(
                0,
                f"## Board of Directors Verdict\n**Action: {action} @ {conf}% confidence**\n{reasoning}"
            )

        sep = "\n\n---\n\n"
        combined = sep.join(sections)
        verdict_text = sep.join(verdict_sections)

        # Truncate to prevent context snowball — research prose only. Verdicts
        # are appended after the cut so they can never be the casualty.
        budget = _MAX_COMPRESSED_CONTEXT_CHARS
        if verdict_text and len(verdict_text) > budget - 600:
            verdict_text = (
                verdict_text[: budget - 600]
                + "\n\n[... verdict TRUNCATED — full artifacts available on SharedDesk ...]"
            )
        if verdict_text:
            notice = "\n\n[... research context TRUNCATED — full artifacts available on SharedDesk ...]"
            if combined and len(combined) + len(sep) + len(verdict_text) > budget:
                keep = max(budget - len(verdict_text) - len(sep) - len(notice), 500)
                combined = combined[:keep] + notice
            combined = combined + sep + verdict_text if combined else verdict_text
        elif len(combined) > budget:
            combined = (
                combined[: budget - 100]
                + "\n\n[... TRUNCATED — full artifacts available on SharedDesk ...]"
            )

        return combined or "No artifacts on desk yet."

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
            "valuation_report": self.valuation_report,
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
        desk.valuation_report = data.get("valuation_report")
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
