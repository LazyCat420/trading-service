"""
The FALLBACK_OUTPUT flag must not fire on ordinary prose (2026-07-31).

cycle-v3-1785504601: 32 of 58 desk artifacts carried FALLBACK_OUTPUT and every
single one was the unanchored `n/?a` pattern matching the substring "na" inside
words like "operational", "narrative", "signals" — a 55% fallback rate that was
100% false positives, on artifacts of 1.9-4.1 KB of grounded prose scored
70-87. The flag feeds guardrails.py, which can force full-panel escalation on
any cycle where a junior-analyst triage lands SKIP/QUANT_ONLY, so a poisoned
signal is armed, not cosmetic.
"""
import pytest

from app.v3.quality_scorer import _detect_failure_patterns


def _patterns_for(summary: str) -> list[str]:
    return _detect_failure_patterns("desk_note", {"summary": summary}, {})


# Real sentences (condensed) from the falsely flagged artifacts of the cycle.
@pytest.mark.parametrize("summary", [
    "Operational margins are narrowing while the turnaround narrative builds.",
    "Signals from Discretionary names remain mixed; guidance was raised.",
    "The tournament analysis shows strong fundamentals.",
])
def test_ordinary_prose_is_not_a_fallback(summary):
    assert "FALLBACK_OUTPUT" not in _patterns_for(summary)


# The flag must still catch what it exists to catch.
@pytest.mark.parametrize("summary", [
    "Revenue: N/A. EPS: n/a.",
    "Fair value estimate: TBD pending data.",
    "Direction unknown at this time.",
    "No data available for this ticker.",
    "Unable to analyze the filing.",
    "This is a placeholder response.",
])
def test_genuine_fallback_phrases_still_flag(summary):
    assert "FALLBACK_OUTPUT" in _patterns_for(summary)
