"""Shadow-mode invariants for the V3 tournament debate.

The tournament is the single largest cost centre in the pipeline — measured
over 14 days at 239,028 tokens and 191s per ticker, 31% of ALL pipeline spend —
and its winner has no measurable relationship to realized P&L:

    bull-won: n=57  mean -0.18%
    bear-won: n=67  mean -0.03%
    difference -0.15%, t = -0.17

That is noise. It is NOT a wiring bug: the winner reaches the Board and visibly
moves it (bull-won -> 65% BUY, bear-won -> 21% BUY), which is exactly why the
experiment needs a switch rather than a deletion.

The switch is delicate for one reason. The same artifact feeds a REAL veto that
fired 12 times in 14 days (HOLD_POLICY_BLOCKED_JURY_VETO). Shadow mode must
suppress the debate's INFLUENCE while leaving its EXECUTION, its artifact and
that veto bit-identical — otherwise the "cheap" configuration is also the one
that silently disarmed a safety gate, and the P&L comparison would be measuring
the wrong difference.

These tests pin the three things that make the experiment valid: the default
does not move, every path the verdict could reach the Board by is closed in
shadow (there are two, and gating only the obvious one would have measured
nothing), and the veto is evaluated from the artifact, downstream of both.
"""

import inspect
from unittest.mock import patch

import pytest

from app.services.parameter_store import PARAMETER_REGISTRY, get_param
from app.v3 import orchestrator
from app.v3.shared_desk import (
    SharedDesk,
    TOURNAMENT_MODE_ACTIVE,
    TOURNAMENT_MODE_SHADOW,
    tournament_debate_mode,
)


def _desk_with_tournament(**overrides):
    """A desk carrying a decided tournament, plus enough research context that
    get_compressed_context returns something either way — otherwise a test
    asserting "the verdict is absent" would pass on an empty string."""
    desk = SharedDesk(ticker="TEST", cycle_id="c1")
    desk.append_artifact("desk_note", {
        "summary": "Junior analyst baseline so the context is never empty.",
        "key_findings": ["revenue up"],
    })
    # _apply_policy_gates blocks on a missing regime BEFORE it ever reaches the
    # jury veto. Without this the veto tests would pass on the wrong gate.
    desk.append_artifact("regime_classification", {
        "summary": "Normal tape.", "regime": "NORMAL", "confidence": 70,
    })
    payload = {
        "summary": "Bull thesis survived head-to-head.",
        "action": "BUY",
        "confidence": 82,
        "winning_side": "bull",
        "pitches": [], "survivors": [],
        "h2h": {"thesis_a": {"persona": "Growth", "attack_points": ["margin risk"]}},
        "jury_verdict": {},
        "vetoed": False,
        "risk_flags": [],
        "total_tokens": 239028,
    }
    payload.update(overrides)
    desk.append_artifact("tournament_result", payload)
    return desk


class TestTheDefaultDoesNotMove:
    def test_registry_default_is_active(self):
        """A shadow default would ship the experiment as live behaviour. The
        parameter exists to run a measurement, not to make a change."""
        assert PARAMETER_REGISTRY["TOURNAMENT_DEBATE_MODE"].default == 0
        assert get_param("TOURNAMENT_DEBATE_MODE") == 0
        assert tournament_debate_mode() == TOURNAMENT_MODE_ACTIVE

    def test_bounds_admit_only_the_two_modes(self):
        """The governor clamps to [min, max]; anything wider would let an agent
        write a value that resolves to neither mode."""
        spec = PARAMETER_REGISTRY["TOURNAMENT_DEBATE_MODE"]
        assert (spec.min_value, spec.max_value) == (0, 1)
        assert spec.kind == "int"

    def test_a_failing_param_lookup_stays_active(self):
        """Fail-open, and note which way "open" points: the shadow branch
        REMOVES evidence the Board reads. A store outage must degrade to
        today's behaviour, never to a silently blinded Board."""
        with patch("app.services.parameter_store.get_param", side_effect=RuntimeError("db down")):
            assert tournament_debate_mode() == TOURNAMENT_MODE_ACTIVE


class TestShadowClosesEveryPathToTheBoard:
    def test_active_renders_the_verdict(self):
        """The control arm. Without this the shadow assertions below could pass
        against a renderer that was already broken."""
        desk = _desk_with_tournament()
        with patch("app.v3.shared_desk.tournament_debate_mode", return_value=TOURNAMENT_MODE_ACTIVE):
            ctx = desk.get_compressed_context(include_debate=True)
        assert "Tournament Debate Verdict" in ctx
        assert "bull" in ctx

    def test_shadow_removes_the_verdict_from_the_desk_context(self):
        """This is the compressed context the Board is prompted with. If the
        winner survives here, the 31% is still buying a decision input and the
        experiment measures nothing."""
        desk = _desk_with_tournament()
        with patch("app.v3.shared_desk.tournament_debate_mode", return_value=TOURNAMENT_MODE_SHADOW):
            ctx = desk.get_compressed_context(include_debate=True)
        assert "Tournament Debate Verdict" not in ctx
        assert "Bull thesis survived" not in ctx
        # ...but the rest of the desk is untouched. Shadow mode suppresses one
        # input, it does not degrade the Board's whole context.
        assert "Junior Analyst Notes" in ctx

    def test_shadow_removes_the_verdict_from_the_whiteboard_summary(self):
        """The SECOND path, and the one that makes gating easy to get wrong.
        The orchestrator also writes tournament_result to the whiteboard, and
        whiteboard.summarize() is injected into every agent's prompt including
        the Board's. Gating only get_compressed_context would leave the verdict
        fully legible and the whole experiment would be a no-op."""
        src = inspect.getsource(orchestrator.run_v3_pipeline)
        assert 'section="tournament_result"' in src, (
            "the whiteboard write moved — re-verify the second leak path"
        )

        from app.agents import whiteboard as wb_mod
        wb_src = inspect.getsource(wb_mod.Whiteboard.summarize)
        assert "tournament_debate_mode" in wb_src, (
            "whiteboard.summarize() no longer gates the debate sections — the "
            "verdict reaches the Board through the prompt regardless of mode"
        )
        assert "debate_judge" in wb_src, (
            "debate_judge is a copy of the tournament verdict; dropping only "
            "tournament_result leaks the same winner under another name"
        )


class TestTheVetoIsUnchangedInBothModes:
    """The veto fired 12 times in 14 days. It must be bit-identical in shadow —
    a cheaper pipeline that also disarmed a safety gate is not the thing being
    measured."""

    @pytest.mark.parametrize("mode", [TOURNAMENT_MODE_ACTIVE, TOURNAMENT_MODE_SHADOW])
    def test_a_vetoed_tournament_blocks_the_trade_in_either_mode(self, mode):
        desk = _desk_with_tournament(vetoed=True)
        desk.append_artifact("final_decision", {
            "summary": "Board wants the trade.",
            "action": "BUY", "confidence": 90,
            "conviction_vector": {"data_quality": 80},
        })
        with patch("app.v3.orchestrator.tournament_debate_mode", return_value=mode), \
             patch("app.v3.shared_desk.tournament_debate_mode", return_value=mode):
            gate = orchestrator._apply_policy_gates(desk)
        assert "JURY_VETO" in str(gate), (
            f"the jury veto did not fire in {mode} mode — shadow must gate "
            f"RENDERING only, never the gate that reads the artifact"
        )

    def test_the_gate_reads_the_artifact_not_the_rendered_context(self):
        """Why the veto is structurally safe: _apply_policy_gates reaches for
        desk.tournament_result directly. Shadow mode never touches the desk
        field — only what get_compressed_context does with it — so no gating
        change can reach this code path."""
        src = inspect.getsource(orchestrator._apply_policy_gates)
        assert 'getattr(desk, "tournament_result", None)' in src
        assert 'tournament.get("vetoed")' in src
        assert "get_compressed_context" not in src
        assert "tournament_debate_mode" not in src, (
            "the policy gate must not become mode-aware — that is exactly the "
            "coupling this design avoids"
        )

    def test_solo_juror_risk_flags_still_demand_mitigation_in_shadow(self):
        """risk_flags ride the same artifact as the veto. Shadow must not
        quietly relax the unmitigated-trade gate either."""
        desk = _desk_with_tournament(risk_flags=["juror_veto_valuation"])
        desk.append_artifact("final_decision", {
            "summary": "Board wants the trade, with no stop and no trigger.",
            "action": "BUY", "confidence": 90,
            "conviction_vector": {"data_quality": 80},
        })
        with patch("app.v3.orchestrator.tournament_debate_mode", return_value=TOURNAMENT_MODE_SHADOW), \
             patch("app.v3.shared_desk.tournament_debate_mode", return_value=TOURNAMENT_MODE_SHADOW):
            gate = orchestrator._apply_policy_gates(desk)
        assert "BLOCKED" in str(gate), (
            "a risk-flagged unmitigated BUY must still hold in shadow mode"
        )


class TestTheArtifactRecordsTheMode:
    def test_the_tournament_write_stamps_shadow_mode(self):
        """The whole point. Without this field on the artifact, shadow cycles
        and active cycles are indistinguishable after the fact and the P&L
        split that justifies the experiment cannot be computed at all."""
        src = inspect.getsource(orchestrator.run_v3_pipeline)
        assert '"shadow_mode": tournament_debate_mode() == TOURNAMENT_MODE_SHADOW' in src

    def test_the_debate_still_runs_and_still_writes_its_artifact(self):
        """Shadow is not a skip. The tournament call and the artifact write must
        both sit OUTSIDE any mode branch, or the veto loses its input and the
        12 blocked trades come back."""
        src = inspect.getsource(orchestrator.run_v3_pipeline)
        assert "await run_tournament_debate(" in src
        assert 'desk.append_artifact("tournament_result"' in src

        # The call and the write must not be conditioned on the mode. Checking
        # the source region between them keeps this honest against a future
        # `if mode == active:` wrapped around the expensive part.
        start = src.index("tournament_result = await run_tournament_debate(")
        end = src.index('"total_tokens": tournament_result.get("total_tokens", 0),')
        region = src[start:end]
        assert "TOURNAMENT_MODE_SHADOW" not in region.replace(
            '"shadow_mode": tournament_debate_mode() == TOURNAMENT_MODE_SHADOW', ""
        ), "the debate's execution or artifact write became mode-conditional"
