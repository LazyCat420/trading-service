"""Every live agent that emits a 0-100 confidence must define the scale.

WHY
---
`confidence` is the most consequential number the desk produces: the policy
gate blocks any BUY/SELL below the floor of 70, ECE reads it as P(correct), and
`decision_outcomes` scores it. An unanchored 0-100 field drifts — between
2026-07-20 and 07-26 the distribution slid 77.1 → 61.6 on a matched population
and desks clearing the floor went 84% → 4%, with the maximum unchanged at 85.
That is a scale moving under accumulated caveat text, not a cap.

The fix that stopped it was prose, not code: a `## WHAT \\`confidence\\` MEANS`
section giving explicit bands (80-90 / 70-79 / 55-69 / below 55). Nine agents
got one. `fundamental_analyst` was the last live holdout — it said what
confidence *meant* ("the share of your thesis you actually verified") but never
mapped it to bands, so its JSON example `"confidence": 0-100` was the only
numeric anchor the model had.

This test pins the invariant so the next agent added to the desk cannot ship
with a naked scale.

NOT COVERED, ON PURPOSE
-----------------------
`app/cognition/debate/thesis_agent.py` and `app/cognition/debate/debate_judge.py`
also emit a bare `"confidence": 0-100`. They are NOT on the live cycle path —
the orchestrator imports `debate_judge` from `app.v3.agents`, and the only
references to the `app/cognition/debate` pair are in
`app/cognition/evolution/target_map.py` (the retired tournament/coral surface).
Anchoring dead prompts would be unmeasurable work; they are listed here so the
omission is a decision on record rather than an oversight.
"""

from __future__ import annotations

import os
import re

import pytest

_AGENTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "app", "v3", "agents",
)

#: The band vocabulary every anchored prompt shares. Matching on the bands
#: rather than on the heading means a prompt can re-word its heading freely,
#: but cannot drop the numbers.
_BAND_PATTERN = re.compile(r"80-90.*?70-79.*?55-69.*?(?:below|under)\s*55", re.S | re.I)

#: Agents that legitimately emit no self-reported confidence.
_NO_CONFIDENCE = {"__init__.py", "portfolio_manager.py"}


def _agent_files() -> list[str]:
    return sorted(
        f for f in os.listdir(_AGENTS_DIR)
        if f.endswith(".py") and f not in _NO_CONFIDENCE
    )


def test_the_agent_directory_is_not_empty():
    """Vacuity guard — a glob that matches nothing passes every check below."""
    files = _agent_files()
    assert len(files) >= 10, f"only found {files!r}; the scan below proved nothing"


@pytest.mark.parametrize("filename", _agent_files())
def test_confidence_emitting_agents_define_their_scale(filename):
    src = open(os.path.join(_AGENTS_DIR, filename), encoding="utf-8").read()

    # Only agents that actually ask the model for a number are in scope.
    asks_for_a_number = re.search(r'"(?:final_)?confidence"\s*:\s*(?:0-100|\d)', src)
    if not asks_for_a_number:
        pytest.skip(f"{filename} emits no numeric confidence")

    assert _BAND_PATTERN.search(src), (
        f"{filename} asks the model for a 0-100 confidence but never says what "
        "the numbers mean. Add a `## WHAT `confidence` MEANS (one scale, "
        "firm-wide)` section with the firm bands (80-90 / 70-79 / 55-69 / "
        "below 55) — see app/v3/agents/quant_analyst.py. An unanchored scale "
        "drifts under caveat text and silently stops clearing the 70 floor."
    )
