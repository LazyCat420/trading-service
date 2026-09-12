"""A gate the harness cannot falsify must REFUSE, not report zero effect.

Open item 53: asking `gate_ablation.py` to ablate
`HOLD_POLICY_BLOCKED_STALE_PRICE_DATA` printed `n_changed_action: 0`. The gate
has no branch in `_ablate`, so the replay ran with the gate still firing —
every desk came back unchanged. Zero there means "never measured", but it reads
as "this gate never matters", which is the more dangerous of the two.

`UNREPLAYABLE` already existed and did not help: it is a hand-maintained
DENYLIST of three gate names, and the gate in the bug report is not one of
them. A denylist misses the next one by construction. The guard these tests pin
asks `_ablate` whether it actually disabled the gate and refuses when it did
not, so the default for an UNKNOWN gate is a refusal.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gate_ablation.py"


def _load():
    spec = importlib.util.spec_from_file_location("_gate_ablation_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _desk():
    """A desk shaped the way `_ablate` expects to read one."""
    return {
        "trade_decision": {"action": "BUY", "confidence": 55},
        "final_decision": {"action": "BUY", "confidence": 55},
        "cycle_metadata": {},
        "tournament_result": {},
    }


def test_the_gate_from_the_bug_report_is_not_ablatable(mod):
    """`_ablate` must report False rather than pretending it disabled it."""
    assert mod._ablate(_desk(), "HOLD_POLICY_BLOCKED_STALE_PRICE_DATA") is False


def test_an_unknown_gate_is_not_ablatable(mod):
    """The DEFAULT for a gate nobody has taught it about is refusal.

    This is the property a denylist cannot have: a gate added to the policy
    tomorrow is covered by this the day it is added.
    """
    assert mod._ablate(_desk(), "HOLD_POLICY_BLOCKED_SOMETHING_INVENTED_TODAY") is False
    assert mod._ablate(_desk(), "") is False


@pytest.mark.parametrize("gate", [
    "HOLD_NO_POSITION",
    "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE",
    "HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT",
])
def test_the_gates_it_DOES_support_still_ablate(mod, gate):
    """The refusal must not swallow the gates the harness exists to measure.

    Without this, 'refuse everything' would pass the tests above and silently
    turn the whole tool off.
    """
    assert mod._ablate(_desk(), gate) is True


def test_low_confidence_ablation_actually_changes_the_desk(mod):
    """Not just a True return — the predicate has to be falsified."""
    d = _desk()
    assert d["trade_decision"]["confidence"] == 55
    mod._ablate(d, "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE")
    assert d["trade_decision"]["confidence"] == 100


# ── the guard itself: these are the tests that pin the FIX ────────────────
# The `_ablate` assertions above were already true before the fix; on their own
# they pin nothing. What changed is that the caller now ASKS.

def test_the_bug_report_gate_is_REFUSED_not_measured(mod):
    """The whole point of open item 53: a verdict, not a zero."""
    v = mod.refusal_for("HOLD_POLICY_BLOCKED_STALE_PRICE_DATA", _desk())
    assert v is not None, "the gate from the bug report must be refused"
    assert v["verdict"] == "needs-more-data"
    assert v["reason"] == mod.NOT_ABLATABLE


def test_an_unknown_gate_is_refused_by_DEFAULT(mod):
    """A denylist misses the next gate; this must not."""
    v = mod.refusal_for("HOLD_POLICY_BLOCKED_INVENTED_TOMORROW", _desk())
    assert v is not None and v["verdict"] == "needs-more-data"


def test_a_supported_gate_is_NOT_refused(mod):
    """Refusing everything would pass every test above and kill the tool."""
    for gate in ("HOLD_NO_POSITION", "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE",
                 "HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT"):
        assert mod.refusal_for(gate, _desk()) is None, gate


def test_an_explicitly_unreplayable_gate_keeps_its_SPECIFIC_reason(mod):
    """The named reason beats the generic one — it tells the operator WHY."""
    v = mod.refusal_for("HOLD_NO_PRICE_DATA", _desk())
    assert v["reason"] == mod.UNREPLAYABLE["HOLD_NO_PRICE_DATA"]
    assert v["reason"] != mod.NOT_ABLATABLE


def test_the_probe_does_not_mutate_the_desk_it_samples(mod):
    """`refusal_for` ablates a COPY. Mutating the real desk would corrupt the
    baseline every later gate is measured against."""
    d = _desk()
    mod.refusal_for("HOLD_POLICY_BLOCKED_LOW_CONFIDENCE", d)
    assert d["trade_decision"]["confidence"] == 55


def test_no_sample_desk_means_no_generic_refusal(mod):
    """A gate that fired on nothing cannot be probed; it must not be refused
    for the wrong reason, but an explicitly unreplayable one still is."""
    assert mod.refusal_for("HOLD_POLICY_BLOCKED_STALE_PRICE_DATA", None) is None
    assert mod.refusal_for("HOLD_NO_PRICE_DATA", None) is not None


def test_the_refusal_reason_says_not_measured_not_no_effect(mod):
    """The words matter: this string is what an operator reads."""
    reason = mod.NOT_ABLATABLE.lower()
    assert "not measured" in reason or "not measured" in reason.replace("'", "")
    assert "_ablate" in mod.NOT_ABLATABLE


def test_unreplayable_still_names_its_three_gates(mod):
    """The explicit list stays — it carries a REASON per gate that the generic
    refusal cannot (live health probe vs point-in-time price read)."""
    assert set(mod.UNREPLAYABLE) == {
        "HOLD_POLICY_BLOCKED_DEGRADED_MODEL",
        "HOLD_NO_PRICE_DATA",
        "DROPPED_IMPLAUSIBLE_LEVEL",
    }
    for reason in mod.UNREPLAYABLE.values():
        assert reason.strip()
