"""One vocabulary for `v3_agent_telemetry.outcome`, expressed as a PERMITTED set.

f48740eb added `PhaseOutcome.CANCELLED` and claimed that closed the
mis-bucketing of cancelled runs. It did not, and the correction is the point of
this file:

  * `PhaseOutcome(value)` coercion appears NOWHERE in production code — no
    validator, Literal, TypedDict or schema enumerates these values — so an
    undefined string never raised and was never "dropped by the enum".
  * `PhaseOutcome.CANCELLED` is never CONSTRUCTED: the cancel path in
    `agent_runner` writes the telemetry STRING and re-raises, so every site
    that takes a `PhaseOutcome` argument is unreachable on a cancel.
  * The rows were mis-bucketed by RAW-STRING readers that never consult the
    enum. Each kept a private DENYLIST tuple of "bad" outcomes and swept
    everything else into an accidental else-branch — which is why the same
    CANCELLED row scored as an LLM failure in `llm_audit`, printed without a
    CRITICAL in `audit-loop`, and drew as an unknown-ish indigo degrade in the
    replay router.

So the fix is the taxonomy: one classification, imported by every reader, with
an import-time guard that fails until a new outcome is given a meaning.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from app.v3.shared_desk import (
    OutcomeClass,
    PhaseOutcome,
    _OUTCOME_CLASS,
    _assert_every_outcome_is_classified,
    classify_outcome,
    outcomes_in,
)

AUDIT_LOOP = Path(__file__).resolve().parents[2] / "scripts" / "audit-loop.py"


def _load_audit_loop():
    spec = importlib.util.spec_from_file_location("_audit_loop_under_test", AUDIT_LOOP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


# ── The permitted set, and the guard that keeps it complete ───────────

def test_every_phase_outcome_has_a_meaning():
    unclassified = [m.value for m in PhaseOutcome
                    if classify_outcome(m) is OutcomeClass.UNRECOGNISED]
    assert unclassified == [], (
        f"{unclassified} is written to telemetry but no reader knows what it "
        f"means — it would land in whichever else-branch each reader has")


def test_the_guard_fires_when_a_new_outcome_is_added_without_a_meaning(monkeypatch):
    """Sabotage: this is the whole 'by construction' claim.

    An allowlist that is only READ drifts as silently as a denylist. The guard
    must reject the state, not merely describe it.
    """
    crippled = {k: v for k, v in _OUTCOME_CLASS.items()
                if k != PhaseOutcome.CANCELLED.value}
    monkeypatch.setattr("app.v3.shared_desk._OUTCOME_CLASS", crippled)
    with pytest.raises(AssertionError, match="CANCELLED"):
        _assert_every_outcome_is_classified()


def test_the_guard_rejects_a_class_for_a_string_nothing_writes(monkeypatch):
    extra = {**_OUTCOME_CLASS, "PROBABLY_FINE": OutcomeClass.DELIVERED}
    monkeypatch.setattr("app.v3.shared_desk._OUTCOME_CLASS", extra)
    with pytest.raises(AssertionError, match="PROBABLY_FINE"):
        _assert_every_outcome_is_classified()


def test_unrecognised_is_never_a_membership_class(monkeypatch):
    bad = {**_OUTCOME_CLASS,
           PhaseOutcome.DATA_GAP.value: OutcomeClass.UNRECOGNISED}
    monkeypatch.setattr("app.v3.shared_desk._OUTCOME_CLASS", bad)
    with pytest.raises(AssertionError, match="UNRECOGNISED"):
        _assert_every_outcome_is_classified()


def test_outcomes_in_partitions_the_vocabulary_both_ways():
    every = set(outcomes_in())
    assert every == {m.value for m in PhaseOutcome}, (
        "set equality in BOTH directions — a count would survive one value "
        "leaving and another arriving")
    buckets = [OutcomeClass.DELIVERED, OutcomeClass.DEGRADED,
               OutcomeClass.FAILED, OutcomeClass.ABANDONED]
    covered: set[str] = set()
    for bucket in buckets:
        members = set(outcomes_in(bucket))
        assert not (members & covered), f"{bucket} overlaps an earlier bucket"
        covered |= members
    assert covered == every
    assert outcomes_in(OutcomeClass.UNRECOGNISED) == ()


# ── The semantics: a cancel is an EXCLUSION, not a failure ────────────

def test_a_cancellation_is_abandoned_not_failed():
    assert classify_outcome("CANCELLED") is OutcomeClass.ABANDONED
    assert "CANCELLED" not in outcomes_in(OutcomeClass.FAILED), (
        "an operator stopping a run — which a deploy does to every in-flight "
        "cycle — is not the model failing to answer")


def test_an_unknown_string_is_unrecognised_not_quietly_fine():
    for value in ("SKIPPED", "?", "", None, "cancelled", "CANCELED"):
        assert classify_outcome(value) is OutcomeClass.UNRECOGNISED, value


def test_the_american_spelling_is_a_different_namespace():
    """`decision_outcomes.outcome` uses CANCELED (one L).

    That is the trade-lifecycle vocabulary in `autoresearch/outcome_tracker`,
    unrelated to a stopped agent run. The two must NOT be unified: folding
    them would make a closed position look like an interrupted LLM call.
    """
    assert "CANCELED" not in outcomes_in()
    assert classify_outcome("CANCELED") is OutcomeClass.UNRECOGNISED


def test_a_phase_outcome_member_classifies_the_same_as_its_string():
    for member in PhaseOutcome:
        assert classify_outcome(member) is classify_outcome(member.value)


# ── Every reader now asks the taxonomy, not a private tuple ───────────

def test_no_reader_keeps_its_own_idea_of_which_outcomes_failed():
    """The drift guard. Three copies of this tuple is how the bug happened."""
    import app.autoresearch.auditors.llm_audit as llm_audit
    import app.routers.cycle_replay_router as replay

    expected = set(outcomes_in(OutcomeClass.FAILED))
    for name, tup in [
        ("llm_audit._FAILED_OUTCOMES", llm_audit._FAILED_OUTCOMES),
        ("cycle_replay_router._FAILED_OUTCOMES", replay._FAILED_OUTCOMES),
        ("audit-loop.FAILED_OUTCOMES", _load_audit_loop().FAILED_OUTCOMES),
    ]:
        assert set(tup) == expected, name
        assert "CANCELLED" not in set(tup), name


def test_llm_audit_excludes_exactly_the_abandoned_class():
    import app.autoresearch.auditors.llm_audit as llm_audit
    assert set(llm_audit._UNSCORED_OUTCOMES) == set(outcomes_in(OutcomeClass.ABANDONED))
    assert set(llm_audit._KNOWN_OUTCOMES) == set(outcomes_in())


# ── audit-loop: the missing `else` ────────────────────────────────────

def test_audit_loop_has_a_line_for_every_class():
    """No outcome may pass through audit-loop without a word said about it."""
    mod = _load_audit_loop()
    seen = {classify_outcome(v): mod.outcome_note("v3_bull_agent", v)
            for v in outcomes_in()}
    assert seen[OutcomeClass.DELIVERED] is None, "SUCCESS needs no extra line"
    for cls in (OutcomeClass.DEGRADED, OutcomeClass.FAILED, OutcomeClass.ABANDONED):
        assert seen.get(cls), f"{cls} prints nothing at all"


def test_audit_loop_does_not_raise_a_critical_on_an_operator_stop():
    mod = _load_audit_loop()
    note = mod.outcome_note("v3_bull_agent", "CANCELLED")
    assert "STOPPED" in note
    assert "CRITICAL" not in note, (
        "a deploy's SIGTERM is not an agent failure — raising CRITICAL here "
        "is the same false alarm llm_audit was raising")


def test_audit_loop_raises_on_an_outcome_it_cannot_classify():
    """The missing `else`. Pre-fix, 'SKIPPED' printed and passed the audit."""
    mod = _load_audit_loop()
    note = mod.outcome_note("v3_quant_analyst", "SKIPPED")
    assert note is not None, (
        "an unhandled outcome silently passing an audit is how the NEXT new "
        "value gets missed")
    assert "CRITICAL" in note
    assert "SKIPPED" in note


def test_audit_loop_still_criticals_the_two_real_failures():
    mod = _load_audit_loop()
    for outcome in outcomes_in(OutcomeClass.FAILED):
        assert "CRITICAL" in mod.outcome_note("v3_bear_agent", outcome)
