"""The evidence partition must fail LOUDLY, not silently.

Information asymmetry is the mechanism that makes multi-agent debate worth its
tokens. Given identical inputs, debate is a martingale — expected correctness
does not improve across rounds — and LLM errors are ~60% correlated, so N agents
reading one packet produce N correlated opinions and call the agreement
consensus.

``filter_packet_for_persona`` implements the partition and has two fallbacks to
the FULL packet: unknown persona, and zero matching facts. Both are deliberate
(a blind persona is worse than an over-informed one, and this must never raise
inside a debate) but both destroy the mechanism.

This has already happened in production: the old ``fact_type`` names matched no
Technical or Macro keyword, the filter fell back for 3 of 4 pitch personas, and
the tournament produced four near-identical pitches from four "independent"
debaters. A warning was logged; the run looked healthy. That is why the
fallbacks are now counted — see ``PARTITION_FALLBACKS`` / ``partition_report``.

These tests pin the counting, not the fallback behaviour itself.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.cognition.contracts.evidence import EvidencePacket, StructuredFact
from app.cognition.debate import debate_coordinator as dc


def _packet(*fact_types: str) -> EvidencePacket:
    now = datetime.now(timezone.utc)
    return EvidencePacket(
        entity_id="TEST",
        structured_facts=[
            StructuredFact(fact_type=t, value="x", timestamp=now) for t in fact_types
        ],
        claims=[],
    )


@pytest.fixture(autouse=True)
def _reset_counters():
    dc.PARTITION_FALLBACKS.clear()
    yield
    dc.PARTITION_FALLBACKS.clear()


def test_partition_holds_for_a_known_persona():
    """The happy path must actually narrow the packet, or nothing else matters."""
    packet = _packet("pe_ratio", "rsi_14", "vix_level")
    out = dc.filter_packet_for_persona(packet, "Fundamental")

    assert len(out.structured_facts) < len(packet.structured_facts)
    assert [f.fact_type for f in out.structured_facts] == ["pe_ratio"]
    assert dc.partition_report()["partitioned"] is True


def test_unknown_persona_is_counted_not_silent():
    """The exact production defect: the tournament's persona names
    (Value_Quant, Momentum_Quant, ...) are not PERSONA_EVIDENCE_FILTER's keys
    (Fundamental, Technical, Macro_Sentiment), so every lookup missed and every
    persona received the full packet. tournament.py carries a second map,
    PITCH_PERSONA_FILTER, solely to work around this.
    """
    packet = _packet("pe_ratio", "rsi_14")
    out = dc.filter_packet_for_persona(packet, "Value_Quant")

    # Fallback behaviour preserved — never blind, never raises.
    assert len(out.structured_facts) == 2

    report = dc.partition_report()
    assert report["partitioned"] is False, "an unpartitioned run must be observable"
    assert report["fallbacks"]["Value_Quant"] == 1


def test_zero_matching_facts_is_counted_not_silent():
    """Right persona, wrong vocabulary — facts exist but none match its keywords."""
    out = dc.filter_packet_for_persona(_packet("desk_note"), "Technical")

    assert len(out.structured_facts) == 1  # full packet returned
    assert dc.partition_report()["fallbacks"]["Technical"] == 1
    assert dc.partition_report()["partitioned"] is False


def test_repeated_fallbacks_accumulate_per_persona():
    """A four-persona debate where three fall back must report 3, not 1 — the
    count is what tells you how much of the debate was really independent."""
    packet = _packet("pe_ratio")
    for name in ("Value_Quant", "Momentum_Quant", "Macro_Quant"):
        dc.filter_packet_for_persona(packet, name)
    dc.filter_packet_for_persona(packet, "Fundamental")  # this one partitions

    report = dc.partition_report()
    assert report["total"] == 3
    assert set(report["fallbacks"]) == {"Value_Quant", "Momentum_Quant", "Macro_Quant"}
    assert report["partitioned"] is False


def test_report_can_be_scoped_to_one_debate_s_personas():
    """A caller should be able to ask about its own personas without being
    tripped by a fallback recorded elsewhere in the process."""
    packet = _packet("pe_ratio")
    dc.filter_packet_for_persona(packet, "Value_Quant")
    dc.filter_packet_for_persona(packet, "Unrelated_Persona")

    scoped = dc.partition_report(personas=["Value_Quant"])
    assert scoped["total"] == 1
    assert scoped["partitioned"] is False

    clean = dc.partition_report(personas=["Fundamental"])
    assert clean["total"] == 0
    assert clean["partitioned"] is True


def test_an_empty_packet_is_not_reported_as_a_fallback():
    """No facts at all is an upstream data problem, not a partition failure.
    Counting it would inflate the signal this report exists to carry."""
    out = dc.filter_packet_for_persona(_packet(), "Technical")

    assert out.structured_facts == []
    assert dc.partition_report()["partitioned"] is True
