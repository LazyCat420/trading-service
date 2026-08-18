"""Decision provenance — a degraded decision must never look like a real one.

Regression cover for the 2026-07-25 finding: 10 desks reached PM_DONE with a
real decision saved to `trade_results` while the desk's `final_decision` stayed
null, because it only propagated on a SUCCESS/DATA_GAP board outcome. Nine were
HOLD, which made it look like a decision bias; it was a write-path bug, and the
HOLD skew came from the degraded path defaulting to HOLD@0.
"""

from __future__ import annotations

import pytest

from app.v3.artifact_validators import coerce_unshortable_sell
from app.v3.shared_desk import (
    DecisionProvenance,
    DeskPhase,
    PhaseOutcome,
    SharedDesk,
)


def _desk() -> SharedDesk:
    return SharedDesk(cycle_id="cycle-test-1", ticker="TEST")


class TestProvenanceStamping:
    def test_unstamped_decision_is_unattributed_not_reasoned(self):
        """A caller that forgets provenance still cannot produce an unmarked
        decision — but it must not be CREDITED either.

        This assertion was inverted until 2026-07-25: the default was
        board_reasoned, so forgetting to stamp was indistinguishable from an
        agent deciding. That fail-open default on the one anti-laundering
        field is what let two hardcoded triage HOLDs into the accuracy
        numbers. Absence of a claim is not a claim.
        """
        desk = _desk()
        desk.append_artifact("final_decision", {"summary": "s", "action": "BUY"})
        assert (
            desk.final_decision["decision_provenance"]
            == DecisionProvenance.UNATTRIBUTED.value
        )

    def test_trade_decision_is_stamped_too(self):
        desk = _desk()
        desk.append_artifact("trade_decision", {"summary": "s", "action": "HOLD"})
        assert (
            desk.trade_decision["decision_provenance"]
            == DecisionProvenance.UNATTRIBUTED.value
        )

    def test_unattributed_is_not_scoreable(self):
        """The point of the honest default: it stays out of accuracy figures."""
        assert (
            DecisionProvenance.UNATTRIBUTED.value
            not in DecisionProvenance.scoreable()
        )

    def test_explicit_provenance_is_preserved(self):
        """A degrade path sets provenance itself; stamping must not clobber it."""
        desk = _desk()
        desk.append_artifact("final_decision", {
            "summary": "board failed",
            "action": None,
            "decision_provenance": DecisionProvenance.BOARD_DEGRADED_FALLBACK.value,
        })
        assert (
            desk.final_decision["decision_provenance"]
            == DecisionProvenance.BOARD_DEGRADED_FALLBACK.value
        )

    def test_coerced_artifact_keeps_coercion_provenance(self):
        """A coercion that ran in agent_runner (before the desk) survives."""
        desk = _desk()
        desk.append_artifact("final_decision", {
            "summary": "s", "action": "HOLD", "_coerced_from": "SELL",
        })
        assert (
            desk.final_decision["decision_provenance"]
            == DecisionProvenance.COERCED_UNSHORTABLE.value
        )

    def test_unknown_provenance_is_replaced_not_trusted(self):
        desk = _desk()
        desk.append_artifact("final_decision", {
            "summary": "s", "action": "BUY", "decision_provenance": "nonsense",
        })
        assert desk.final_decision["decision_provenance"] in {
            p.value for p in DecisionProvenance
        }

    def test_non_decision_artifacts_are_not_stamped(self):
        """Only artifacts carrying a tradeable action need provenance."""
        desk = _desk()
        desk.append_artifact("quant_report", {"summary": "s"})
        assert "decision_provenance" not in desk.quant_report

    def test_only_board_reasoned_is_scoreable(self):
        """Enumerated exhaustively on purpose: a new member added to the enum
        must not be able to drift into the scoreable set unnoticed."""
        scoreable = DecisionProvenance.scoreable()
        assert scoreable == {DecisionProvenance.BOARD_REASONED.value}
        for p in (
            DecisionProvenance.BOARD_DEGRADED_FALLBACK,
            DecisionProvenance.COERCED_UNSHORTABLE,
            DecisionProvenance.TIMEOUT_ABORT,
            DecisionProvenance.NO_TRADE_GATE_SKIP,
            DecisionProvenance.TRIAGE_SKIP,
            DecisionProvenance.UNATTRIBUTED,
        ):
            assert p.value not in scoreable


class TestTriageSkipsAreNotBoardOpinions:
    """2026-07-25: the Triage Gate glance-skip and the JA triage SKIP both
    wrote a hardcoded HOLD@0 with no provenance. The old permissive default
    stamped them board_reasoned, so the scorecard scored a heuristic that ran
    BEFORE any agent as though the board had reasoned to a HOLD.
    """

    def test_triage_skip_is_not_scoreable(self):
        assert (
            DecisionProvenance.TRIAGE_SKIP.value
            not in DecisionProvenance.scoreable()
        )

    def test_triage_skip_survives_stamping(self):
        """An explicit triage stamp must not be rewritten by append_artifact."""
        desk = _desk()
        desk.append_artifact("final_decision", {
            "action": "HOLD", "confidence": 0,
            "persona_used": "Triage Gate",
            "decision_provenance": DecisionProvenance.TRIAGE_SKIP.value,
        })
        assert (
            desk.final_decision["decision_provenance"]
            == DecisionProvenance.TRIAGE_SKIP.value
        )

    def test_triage_skip_is_excluded_by_the_scorecard_rule(self):
        desk = {"final_decision": {"decision_provenance": "triage_skip"}}
        assert _resolve_provenance(desk) == "triage_skip"

    def test_triage_skip_is_a_skip_not_a_degrade(self):
        """A deliberate skip is a correct outcome, not a pipeline failure.

        If this flips, a healthy glance-skip renders as action=DEGRADED, the
        memory store learns an outcome_label of DEGRADED, and the policy gate
        returns HOLD_DEGRADED_NO_DECISION — on the cheapest, highest-volume
        route in the pipeline.
        """
        from app.v3.orchestrator import _is_degraded_decision

        assert not _is_degraded_decision({
            "action": "HOLD", "confidence": 0,
            "decision_provenance": DecisionProvenance.TRIAGE_SKIP.value,
        })

    def test_board_degraded_is_still_a_degrade(self):
        from app.v3.orchestrator import _is_degraded_decision

        assert _is_degraded_decision({
            "action": None,
            "decision_provenance": DecisionProvenance.BOARD_DEGRADED_FALLBACK.value,
        })

    def test_triage_skip_hold_still_gates_as_no_signal(self):
        from app.v3.orchestrator import _apply_policy_gates

        desk = _desk()
        desk.append_artifact("final_decision", {
            "action": "HOLD", "confidence": 0,
            "persona_used": "Triage Gate",
            "decision_provenance": DecisionProvenance.TRIAGE_SKIP.value,
        })
        assert _apply_policy_gates(desk) == "HOLD_NO_SIGNAL"


def _resolve_provenance(desk: dict) -> str | None:
    """Mirror of the scorecard's resolution rule (scripts/agent_scorecard.py).

    Kept in sync by hand: the script is not importable as a module, and the
    rule is subtle enough to be worth pinning.
    """
    found = [
        (desk.get(k) or {}).get("decision_provenance")
        for k in ("final_decision", "trade_decision")
    ]
    found = [p for p in found if p]
    bad = [p for p in found if p != "board_reasoned"]
    return bad[0] if bad else (found[0] if found else None)


class TestProvenanceResolutionAcrossArtifacts:
    """An unstamped artifact must never mask a stamped degrade.

    The backfilled desks caught this: their `trade_decision` predates the
    field, so a filter reading only the first non-empty artifact scored a
    known-degraded desk as if an agent had decided it.
    """

    def test_degraded_final_decision_wins_over_unstamped_trade_decision(self):
        desk = {
            "final_decision": {"decision_provenance": "board_degraded_fallback"},
            "trade_decision": {"action": "HOLD"},  # pre-provenance artifact
        }
        assert _resolve_provenance(desk) == "board_degraded_fallback"

    def test_degraded_trade_decision_wins_over_reasoned_final(self):
        desk = {
            "final_decision": {"decision_provenance": "board_reasoned"},
            "trade_decision": {"decision_provenance": "coerced_unshortable"},
        }
        assert _resolve_provenance(desk) == "coerced_unshortable"

    def test_all_reasoned_resolves_reasoned(self):
        desk = {
            "final_decision": {"decision_provenance": "board_reasoned"},
            "trade_decision": {"decision_provenance": "board_reasoned"},
        }
        assert _resolve_provenance(desk) == "board_reasoned"

    def test_legacy_desk_resolves_none(self):
        """Pre-2026-07-25 desks are unknown, not degraded — they stay in."""
        desk = {"final_decision": {"action": "BUY"}, "trade_decision": {"action": "BUY"}}
        assert _resolve_provenance(desk) is None


class TestProvenanceReachesTradeResults:
    """The desk is not the only record. `trade_results` is what the replay API,
    the UI and the freshness gate read, and it was blind to provenance for a
    full wave — a degraded fallback rendered as a confident verdict everywhere
    a human actually looks.
    """

    def _save(self, verdict: dict) -> dict:
        """Run the saver and return the `trade_results` document it wrote.

        The saver writes through `mongo_store.insert_docs` now, so the old
        `get_db` patch intercepted nothing and the assertions were scored
        against a FakeDB that never saw a call. Asserting on the document is
        also stronger than the old substring checks against SQL text: the
        field has to carry the right VALUE under the right KEY, not merely
        appear somewhere in a statement.
        """
        from unittest.mock import MagicMock, patch

        import app.services.trade_result_saver as trs

        store = MagicMock()
        with patch.object(trs, "mongo_store", store):
            trs.save_trade_result("T", "c1", verdict)

        store.insert_docs.assert_called_once()
        collection, docs = store.insert_docs.call_args[0][:2]
        assert collection == "trade_results"
        # The write is an upsert-by-hand: the prior row for this ticker+cycle
        # is removed first, or a re-run duplicates the decision.
        store.delete_docs.assert_called_once_with(
            "trade_results", {"ticker": "T", "cycle_id": "c1"}
        )
        assert len(docs) == 1
        return docs[0]

    def test_provenance_is_persisted(self):
        doc = self._save({
            "action": "HOLD", "confidence": 60,
            "decision_provenance": "board_degraded_fallback",
        })
        assert doc["decision_provenance"] == "board_degraded_fallback"

    def test_missing_provenance_is_null_not_defaulted(self):
        """Absent means UNKNOWN. Defaulting it to board_reasoned would assert
        an agent decided when nothing recorded that."""
        doc = self._save({"action": "HOLD", "confidence": 60})
        # The key is still written — absent from the document would read as
        # "this deployment has no provenance", not "this decision has none".
        assert "decision_provenance" in doc
        assert doc["decision_provenance"] is None

    def test_blank_provenance_is_normalized_to_null(self):
        doc = self._save({
            "action": "HOLD", "confidence": 60, "decision_provenance": "   ",
        })
        assert doc["decision_provenance"] is None

    def test_non_string_provenance_is_rejected(self):
        doc = self._save({
            "action": "HOLD", "confidence": 60, "decision_provenance": 123,
        })
        assert doc["decision_provenance"] is None


class TestTerminalPhaseRecorded:
    def test_terminal_phase_appears_in_phase_outcomes(self):
        """852/852 desks carried a `phase` absent from `phase_outcomes`, which
        made "did the PM stage run?" unanswerable."""
        desk = _desk()
        desk.advance_phase(DeskPhase.RESEARCH_DONE)
        desk.advance_phase(DeskPhase.DEBATE_DONE)
        desk.advance_phase(DeskPhase.PM_DONE)
        assert "PM_DONE" in desk.phase_outcomes

    def test_aborted_is_recorded_too(self):
        desk = _desk()
        desk.advance_phase(DeskPhase.ABORTED, PhaseOutcome.TIMED_OUT)
        assert "ABORTED" in desk.phase_outcomes

    def test_non_terminal_phases_still_grade_the_phase_left(self):
        desk = _desk()
        desk.advance_phase(DeskPhase.RESEARCH_DONE, PhaseOutcome.DATA_GAP)
        assert desk.phase_outcomes["INIT"] == PhaseOutcome.DATA_GAP.value


class TestCoercionIsCountable:
    def test_coercion_stamps_provenance(self):
        art = coerce_unshortable_sell(
            {"action": "SELL", "confidence": 80}, held=False, ticker="AMD",
        )
        assert art["action"] == "HOLD"
        assert art["_coerced_from"] == "SELL"
        assert art["decision_provenance"] == "coerced_unshortable"

    def test_held_sell_is_untouched(self):
        art = coerce_unshortable_sell(
            {"action": "SELL", "confidence": 80}, held=True, ticker="AXP",
        )
        assert art["action"] == "SELL"
        assert "decision_provenance" not in art

    def test_ticker_is_optional_for_backwards_compat(self):
        """Existing call sites pass only (artifact, held=)."""
        art = coerce_unshortable_sell({"action": "SELL"}, held=False)
        assert art["action"] == "HOLD"

    @pytest.mark.parametrize("action", ["BUY", "HOLD", ""])
    def test_non_sell_untouched(self, action):
        art = coerce_unshortable_sell({"action": action}, held=False)
        assert art.get("action") == action
