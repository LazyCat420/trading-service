"""The probabilistic panel — behaviour that must hold without an LLM in the loop.

The old tournament's aggregation lived inside a 790-line async function and was
never tested against a known answer. These tests drive the real panel with a
stubbed ``llm.chat`` so the contract is checkable: the artifact shape downstream
consumers depend on, the failure modes, and above all that a run whose evidence
partition silently collapsed is *marked* rather than averaged in.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.cognition.contracts.evidence import EvidencePacket, StructuredFact
from app.cognition.debate import probabilistic_panel as pp
from app.cognition.debate.debate_coordinator import PERSONA_EVIDENCE_FILTER

# The fact_type names the orchestrator actually builds. Each must hit exactly
# one filter category — that mapping IS the partition.
PROD_FACTS = (
    "fundamental_report",
    "fundamental_valuation_note",
    "technical_quant_report",
    "positioning_news_desk_note",
    "macro_regime_note",
)


def _packet(*fact_types: str) -> EvidencePacket:
    now = datetime.now(timezone.utc)
    return EvidencePacket(
        entity_id="TEST",
        structured_facts=[
            StructuredFact(fact_type=t, value=f"value for {t}", timestamp=now)
            for t in (fact_types or PROD_FACTS)
        ],
        claims=[],
    )


def _reply(p: float, reasoning: str = "because the data says so"):
    return (json.dumps({
        "probability": p, "reasoning": reasoning,
        "key_evidence": "a figure", "what_would_change_my_mind": "the opposite",
    }), 100, 50)


@pytest.fixture(autouse=True)
def _clear_partition_counters():
    from app.cognition.debate import debate_coordinator as dc
    dc.PARTITION_FALLBACKS.clear()
    yield
    dc.PARTITION_FALLBACKS.clear()


class TestPartitionIsTheMechanism:
    """Information asymmetry is not a nicety — with identical inputs debate is a
    martingale and LLM errors are ~60% correlated. If the partition collapses,
    the panel is an ensemble of near-duplicates wearing four names."""

    def test_the_four_analysts_map_to_four_distinct_categories(self):
        keys = [c["filter_key"] for c in pp.PANEL_ANALYSTS.values()]
        assert len(keys) == len(set(keys)) == 4, (
            "the old tournament had 4 personas over 3 categories, so two read "
            "the identical single fact"
        )
        for k in keys:
            assert k in PERSONA_EVIDENCE_FILTER

    def test_production_facts_split_disjointly_across_the_panel(self):
        """No fact may reach two analysts, and every fact must reach one.
        Overlap means two 'independent' views share evidence."""
        from app.cognition.debate.debate_coordinator import filter_packet_for_persona

        packet = _packet()
        seen: dict[str, set[str]] = {}
        for name, cfg in pp.PANEL_ANALYSTS.items():
            out = filter_packet_for_persona(packet, cfg["filter_key"])
            seen[name] = {f.fact_type for f in out.structured_facts}
            assert seen[name], f"{name} received an empty slice"

        names = list(seen)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                assert not (seen[a] & seen[b]), f"{a} and {b} share {seen[a] & seen[b]}"

        assert set().union(*seen.values()) == set(PROD_FACTS), "a fact reaches nobody"

    @pytest.mark.asyncio
    async def test_a_clean_run_reports_partitioned_true(self):
        with patch.object(pp.llm, "chat", new=AsyncMock(return_value=_reply(0.6))):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)
        assert out["partitioned"] is True
        assert out["partition_fallbacks"] == {}

    @pytest.mark.asyncio
    async def test_a_collapsed_partition_is_marked_not_hidden(self):
        """Facts whose types match no filter keyword make every analyst fall back
        to the FULL packet. That run is N agents on one packet — the exact state
        that makes debate worthless — and it must be visible to the scorer."""
        with patch.object(pp.llm, "chat", new=AsyncMock(return_value=_reply(0.6))):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet("mystery_blob", "another_blob"),
                cycle_id="c1", rounds=1)
        assert out["partitioned"] is False
        assert out["partition_fallbacks"], "the fallback must be counted per analyst"

    @pytest.mark.asyncio
    async def test_the_shared_evidence_control_is_never_partitioned(self):
        """rho=1.0. If Brier is unchanged against this, the asymmetry is not
        doing the work and the panel is just an ensemble."""
        with patch.object(pp.llm, "chat", new=AsyncMock(return_value=_reply(0.6))):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1",
                shared_evidence=True, rounds=1)
        assert out["shared_evidence_control"] is True
        assert out["partitioned"] is False
        assert all(v["facts_seen"] == len(PROD_FACTS) for v in out["views"])


class TestArtifactContract:
    """Downstream consumers read these fields. Breaking one stalls the pipeline
    or silently empties the UI."""

    @pytest.mark.asyncio
    async def test_emits_the_fields_consumers_require(self):
        with patch.object(pp.llm, "chat", new=AsyncMock(return_value=_reply(0.75))):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)

        for field in ("summary", "action", "confidence", "winning_side",
                      "vetoed", "risk_flags", "total_tokens", "probability"):
            assert field in out, f"missing {field}"

        assert out["action"] in ("BUY", "SELL", "HOLD")
        assert 0 <= out["confidence"] <= 100
        # agent_scorecard._DIRECTION_MAP only speaks this vocabulary.
        assert out["winning_side"] in ("bull", "bear", "split", "fallback")

    @pytest.mark.asyncio
    async def test_probability_drives_action_and_confidence(self):
        with patch.object(pp.llm, "chat", new=AsyncMock(return_value=_reply(0.85))):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)
        assert out["probability"] > 0.5
        assert out["action"] == "BUY"
        assert out["winning_side"] == "bull"
        assert out["confidence"] > 0

    @pytest.mark.asyncio
    async def test_a_neutral_panel_holds_with_zero_confidence(self):
        with patch.object(pp.llm, "chat", new=AsyncMock(return_value=_reply(0.5))):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)
        assert out["action"] == "HOLD"
        assert out["confidence"] == 0
        assert "neutral" in out["summary"].lower()

    @pytest.mark.asyncio
    async def test_disagreement_is_recorded(self):
        """A pooled 0.55 from [0.54,0.55,0.56] and one from [0.15,0.95] mean
        different things. The tournament threw this away."""
        replies = [_reply(0.15), _reply(0.95), _reply(0.55), _reply(0.50)]
        with patch.object(pp.llm, "chat", new=AsyncMock(side_effect=replies)):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)
        assert out["disagreement"] > 0.5


class TestFailureModes:
    @pytest.mark.asyncio
    async def test_one_dead_analyst_does_not_kill_the_panel(self):
        replies = [Exception("boom"), _reply(0.7), _reply(0.65), _reply(0.6)]
        with patch.object(pp.llm, "chat", new=AsyncMock(side_effect=replies)):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)
        assert out["analysts_responded"] == 3
        assert out["analysts_expected"] == 4
        assert out["action"] in ("BUY", "SELL", "HOLD")

    @pytest.mark.asyncio
    async def test_unparseable_output_is_dropped_not_defaulted_to_neutral(self):
        """An abstention and a parse failure are different events. Silently
        converting the second into the first makes a broken agent an invisible
        vote for 'no view'."""
        replies = [("not json at all", 10, 5), _reply(0.8), _reply(0.8), _reply(0.8)]
        with patch.object(pp.llm, "chat", new=AsyncMock(side_effect=replies)):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)
        assert out["analysts_responded"] == 3
        assert out["probability"] > 0.7, "a dropped analyst must not drag the pool"

    @pytest.mark.asyncio
    async def test_total_failure_is_labelled_degraded_not_a_verdict(self):
        """A panel that could not run must not read as a panel that saw no
        signal — that is the laundering this codebase keeps re-learning."""
        with patch.object(pp.llm, "chat", new=AsyncMock(side_effect=Exception("down"))):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)
        assert out["degraded"] is True
        assert out["action"] == "HOLD"
        assert out["confidence"] == 0
        assert out["winning_side"] == "fallback"

    @pytest.mark.asyncio
    async def test_percent_scale_answers_are_accepted(self):
        """A model that answers 62 meaning 62% must not be read as p=1.0."""
        reply = (json.dumps({"probability": 62, "reasoning": "r"}), 10, 5)
        with patch.object(pp.llm, "chat", new=AsyncMock(return_value=reply)):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)
        assert 0.55 < out["probability"] < 0.7


class TestDeliberation:
    @pytest.mark.asyncio
    async def test_two_rounds_run_revision_and_keep_round_one(self):
        """Both rounds must be retained: the round-1 -> round-2 delta is how we
        find out whether deliberation earned its tokens at all."""
        with patch.object(pp.llm, "chat", new=AsyncMock(return_value=_reply(0.7))):
            out = await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=2)
        assert out["rounds"] == 2
        assert len(out["round1_views"]) == out["analysts_responded"]

    @pytest.mark.asyncio
    async def test_single_round_skips_revision(self):
        chat = AsyncMock(return_value=_reply(0.7))
        with patch.object(pp.llm, "chat", new=chat):
            await pp.run_probabilistic_panel(
                ticker="TEST", packet=_packet(), cycle_id="c1", rounds=1)
        assert chat.await_count == len(pp.PANEL_ANALYSTS), "no revision calls expected"


class TestOrchestratorWiring:
    """DEBATE_ENGINE must gate the CALL, not the rendering.

    TOURNAMENT_DEBATE_MODE's shadow branch was measured to save ZERO tokens:
    run_tournament_debate is invoked unconditionally there and only the prompt
    section is filtered, so the experiment cost the same either way. These tests
    pin that the new switch does not repeat it.
    """

    def test_engine_selection_gates_the_call(self):
        import inspect
        from app.v3 import orchestrator

        src = inspect.getsource(orchestrator.run_v3_pipeline)
        assert "DEBATE_ENGINE" in src
        assert "run_probabilistic_panel" in src
        # Exactly one engine per ticker: the panel call must sit in the branch
        # that excludes the tournament call, not alongside it.
        assert "else:" in src and "run_tournament_debate" in src

    def test_engine_lookup_fails_open_to_the_default_engine(self):
        """A parameter miss must land on the CHOSEN behaviour.

        Updated 2026-07-29 with the retirement of the tournament (engine 3 is
        now the default — see the measurement block on DEBATE_ENGINE in
        app/services/parameter_store.py). This test used to require
        `_engine = 0`, which was correct while 0 was the default. With the
        default at 3 that same assertion would require a transient
        parameter-store hiccup to silently resurrect 28.2% of pipeline spend,
        so the invariant is "fail open to the default", not "fail open to the
        tournament".
        """
        import inspect
        from app.v3 import orchestrator

        src = inspect.getsource(orchestrator.run_v3_pipeline)
        assert "DEBATE_ENGINE lookup failed" in src
        # Two lookups now: the engine-3 gate in _queue_debate_phase and the
        # engine selector in _execute_tournament_debate. BOTH must fail open to
        # the default, or a transient store error resurrects the tournament.
        sites, i = [], src.find('_get_engine("DEBATE_ENGINE")')
        while i != -1:
            sites.append(src[i:i + 400])
            i = src.find('_get_engine("DEBATE_ENGINE")', i + 1)
        assert len(sites) >= 2, f"expected 2 lookups, found {len(sites)}"
        for n, window in enumerate(sites):
            assert "= 3" in window, f"lookup #{n} must fail open to no-debate"
            assert "_engine = 0" not in window, f"lookup #{n} falls back to tournament"
            assert "_engine_sel = 0" not in window

    def test_registry_admits_exactly_the_four_engines(self):
        """0=tournament, 1=panel, 2=panel/shared-evidence, 3=no debate.

        Engines 0-2 stay selectable so the comparison can be re-run; 3 is the
        default because the tournament did not beat the free quant signal.
        """
        from app.services.parameter_store import PARAMETER_REGISTRY

        spec = PARAMETER_REGISTRY["DEBATE_ENGINE"]
        assert spec.default == 3, "the tournament must not run by default"
        assert (spec.min_value, spec.max_value) == (0, 3)

    def test_vetoed_is_read_from_the_engine_not_only_the_jury(self):
        """The tournament reports `vetoed` inside jury_verdict; the panel has no
        jury and reports it at the top level. Reading only the nested path would
        silently coerce it to False for any engine without a jury — the same
        shape as a fallback that looks like a real answer."""
        import inspect
        from app.v3 import orchestrator

        src = inspect.getsource(orchestrator.run_v3_pipeline)
        assert 'tournament_result.get(\n                        "vetoed"' in src or \
               'tournament_result.get("vetoed"' in src

    def test_panel_only_fields_are_carried_onto_the_artifact(self):
        """`probability` is the real signal — `confidence` is derived from it.
        If it is not persisted, the panel cannot be scored and the rebuild is
        as unfalsifiable as the tournament was."""
        import inspect
        from app.v3 import orchestrator

        src = inspect.getsource(orchestrator.run_v3_pipeline)
        for field in ("probability", "partitioned", "disagreement", "engine"):
            assert field in src, f"artifact must carry {field}"
