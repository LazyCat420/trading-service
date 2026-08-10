"""The silent INIT -> PM_DONE loss: a rejected artifact type kills the desk.

WHAT ACTUALLY HAPPENS IN PRODUCTION
-----------------------------------
Confirmed on CRH (cycle-v3-1786346325), ADBE (cycle-v3-1786352635),
ASML (cycle-v3-1786368600), and ABNB/SNDK/META (cycle-v3-1786386399):

  1. The Junior Analyst finishes SUCCESS and publishes `desk_note` to the
     whiteboard  (app/v3/orchestrator.py:1962).
  2. `whiteboard_subscriber` (app/v3/orchestrator.py:907) receives it, reads
     `triage_recommendation == "QUANT_ONLY"`, and calls
     `research_degraded()` (app/v3/orchestrator.py:1038). It returns a reason
     — the JA's own tool calls failed, or its data_gaps name a tool failure.
  3. The override to FULL begins. Its FIRST persistent action is
     `desk.append_artifact("degradation_note", {...})`
     (app/v3/orchestrator.py:1047).
     `degradation_note` is NOT in `_VALID_ARTIFACT_TYPES`
     (app/v3/shared_desk.py:172-186), so `append_artifact` RAISES
     (app/v3/shared_desk.py:256-260).
  4. `Whiteboard._notify_subscribers` swallows it
     (app/agents/whiteboard.py:83-84) with a warning that names neither the
     ticker, nor the cycle, nor the section. No `execution_errors` row, no
     AGENT_ERROR telemetry.
  5. Everything after line 1047 never runs: `triage = "FULL"` (1053), the
     `fa_skipped` clear (1082, the whole point of the 2026-07-29 ORCL fix),
     and all three dispatch branches (1085 / 1106 / 1126). No FA, no QA, no
     valuation, no debate, no board.
  6. The desk stays at INIT with no `final_decision`, so
     app/v3/orchestrator.py:2267 takes the `else` and calls
     `advance_phase(DeskPhase.PM_DONE)` (2273) -> ValueError, caught at 2275,
     converted into a DEGRADED sentinel.
  7. `_build_v1_compatible_result` (app/v3/orchestrator.py:3625) then writes a
     row that CLAIMS the whole pipeline ran: `escalated: True`,
     `stages_completed: [regime_classification, research, debate, decision]`,
     `integrity_status: "passed"` — on a desk that ran exactly one agent.

22 override attempts are recorded in `pipeline_events` since 2026-07-28.
ZERO desks in the whole database carry a `degradation_note`. The override has
never once completed.

WHY THESE TESTS LOOK LIKE THIS
------------------------------
Asserting that the state machine rejects INIT -> PM_DONE proves nothing: the
state machine is correct and its rejection is the only reason we know anything
went wrong. The defect is that BOTH rejections (the artifact type, then the
phase transition) are swallowed and reappear as a confident-looking artifact.

So: no test here restates orchestrator logic. `test_no_orchestrator_call_site_
appends_an_unknown_artifact_type` walks the real AST of the real orchestrator,
and `test_triage_override_survives_its_own_degradation_note` feeds the literal
IT found into the real `SharedDesk` behind the real `Whiteboard` fan-out. If
the call site is renamed or moved, the tests follow it.

    python -m pytest tests/unit/test_desk_phase_transition.py -q
"""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agents.whiteboard import Whiteboard
from app.v3 import orchestrator
from app.v3.orchestrator import _build_v1_compatible_result
from app.v3.shared_desk import (
    _VALID_ARTIFACT_TYPES,
    DeskPhase,
    SharedDesk,
)

_ORCHESTRATOR_SRC = Path(inspect.getfile(orchestrator))


# ── Reading the real call sites, not a copy of them ──────────────────────


def _append_artifact_call_sites() -> list[tuple[str, int]]:
    """(artifact_type, lineno) for every literal `…append_artifact("x", …)`
    in the real orchestrator source.

    Uses the ast.Call node that OWNS the argument, so a string that merely
    sits near an append cannot be mistaken for one.
    """
    tree = ast.parse(_ORCHESTRATOR_SRC.read_text(), filename=str(_ORCHESTRATOR_SRC))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "append_artifact"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append((first.value, node.lineno))
    return found


def _rejected_call_sites() -> list[tuple[str, int]]:
    return [
        (name, line)
        for name, line in _append_artifact_call_sites()
        if name not in _VALID_ARTIFACT_TYPES
    ]


def _triage_override_append() -> tuple[str, int]:
    """The artifact type the triage-override branch appends, found by the
    SHAPE of its payload (`applied_triage`), not by its current name — so
    renaming the artifact does not silently retire the test below.
    """
    tree = ast.parse(_ORCHESTRATOR_SRC.read_text(), filename=str(_ORCHESTRATOR_SRC))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "append_artifact"):
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Dict):
            continue
        keys = {
            k.value for k in node.args[1].keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if "applied_triage" in keys and isinstance(node.args[0], ast.Constant):
            return node.args[0].value, node.lineno
    raise AssertionError(
        "no append_artifact call carrying an 'applied_triage' payload found in "
        f"{_ORCHESTRATOR_SRC} — the triage-override branch moved; retarget this test"
    )


# ── 1. The precondition: an artifact type the desk will not accept ───────


def test_no_orchestrator_call_site_appends_an_unknown_artifact_type():
    """Every artifact the orchestrator appends must be one the desk accepts.

    `SharedDesk.append_artifact` raises on an unknown type. A call site that
    passes one is not a validation warning — it is an unconditional crash on
    whatever code path reaches it, and here that path is the triage-override
    that exists to protect the desk from degraded research.
    """
    offenders = _rejected_call_sites()
    assert offenders == [], (
        "orchestrator appends artifact type(s) SharedDesk rejects: "
        + ", ".join(
            f"{name!r} at {_ORCHESTRATOR_SRC.name}:{line}" for name, line in offenders
        )
        + f"\nValid types: {sorted(_VALID_ARTIFACT_TYPES)}"
    )


# ── 2. The swallow: the rejection kills the rest of the subscriber ───────


@pytest.mark.asyncio
async def test_triage_override_survives_its_own_degradation_note():
    """The statements AFTER the degradation-note append must still run.

    This is the production shape, reduced to its two real objects: a real
    `SharedDesk`, and the real `Whiteboard` fan-out that carries the
    `desk_note` event to the subscriber. The subscriber appends the artifact
    type the REAL orchestrator appends (read out of its AST above), then
    records that it got past that line — standing in for `triage = "FULL"`
    (orchestrator.py:1053) and the `fa_skipped` clear (orchestrator.py:1082),
    neither of which has executed in production since the override shipped.
    """
    artifact_type, _line = _triage_override_append()

    desk = SharedDesk(cycle_id="cycle-test-1", ticker="CRH")
    reached_dispatch: list[str] = []

    async def subscriber(event):
        if event.get("section") != "desk_note":
            return
        # The override body, in the order the orchestrator has it.
        desk.append_artifact(artifact_type, {
            "stage": "junior_analyst_triage",
            "requested_triage": "QUANT_ONLY",
            "applied_triage": "FULL",
            "reason": "1 failed execute_javascript call(s) this cycle",
        })
        reached_dispatch.append("FULL")

    board = Whiteboard()
    board.subscribe(subscriber, ticker="CRH")

    with patch("app.agents.whiteboard.get_db") as mock_get_db:
        db = MagicMock()
        db.transaction.return_value = MagicMock()
        mock_get_db.return_value.__enter__.return_value = db
        db.execute.return_value.fetchone.side_effect = [None, (1,)]
        await board.write_section(
            ticker="CRH",
            cycle_id="cycle-test-1",
            section="desk_note",
            content={"triage_recommendation": "QUANT_ONLY", "summary": "…"},
            author_agent="v3_junior_analyst",
        )

    assert reached_dispatch == ["FULL"], (
        "the triage override died on its own append_artifact call and the "
        "whiteboard swallowed it — no analyst was ever dispatched, and "
        "nothing downstream can tell that this happened"
    )


# ── 3. The silence: the one log line that would have shown this ──────────


@pytest.mark.asyncio
async def test_swallowed_subscriber_failure_names_the_ticker(caplog):
    """A swallowed subscriber crash must say WHICH ticker it just lost.

    `_notify_subscribers` logs `"Dynamic subscriber callback failed: %s"` and
    nothing else — no ticker, no cycle_id, no section, no traceback. Grepping
    a week of container logs for that line cannot tell an operator that CRH
    lost its whole research panel, which is why 22 of these were invisible.
    """
    board = Whiteboard()

    async def exploding(event):
        raise ValueError("Invalid artifact_type: degradation_note")

    board.subscribe(exploding, ticker="CRH")
    with caplog.at_level(logging.WARNING, logger="app.agents.whiteboard"):
        await board._notify_subscribers({
            "type": "whiteboard_update",
            "ticker": "CRH",
            "cycle_id": "cycle-test-1",
            "section": "desk_note",
        })

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text, "the subscriber crash was not logged at all"
    missing = [f for f in ("CRH", "cycle-test-1", "desk_note") if f not in text]
    assert not missing, (
        f"the swallow log omits {missing} — an operator cannot attribute the "
        f"failure to a ticker or a cycle. Logged: {text!r}"
    )


# ── 4. The artifact: a one-agent desk presented as a completed pipeline ──


def test_lost_desk_is_not_reported_as_a_completed_pipeline():
    """The DEGRADED result must not claim research/debate/decision ran.

    This is the exact stored shape of CRH in cycle-v3-1786346325: phase INIT,
    a single desk_note, and the sentinel `final_decision` written by the
    `except ValueError` handler at orchestrator.py:2275-2298. Passing it
    through the REAL `_build_v1_compatible_result` reproduces the row that
    landed in `analysis_results`:

        "escalated": true,
        "v2_metadata": {"debate": {"integrity_status": "passed", …},
                        "stages_completed": ["regime_classification",
                                             "research", "debate", "decision"]}

    Only the `action` string says anything went wrong. Every other field
    asserts a full pipeline that never ran.
    """
    desk = SharedDesk(cycle_id="cycle-v3-1786346325", ticker="CRH")
    desk.append_artifact("desk_note", {
        "summary": "CRH is unchanged in substance from the prior 8/6 cycle…",
        "triage_recommendation": "QUANT_ONLY",
        "data_gaps": ["DataGap: exact HSR/antitrust milestone date unavailable"],
    })
    desk.append_artifact("final_decision", {
        "action": None,
        "confidence": 0,
        "reasoning": (
            "Pipeline ended at DeskPhase.INIT without producing a decision: "
            "Invalid transition: INIT → PM_DONE. "
            "Valid targets: ['ABORTED', 'RESEARCH_DONE']"
        ),
        "decision_provenance": "board_degraded_fallback",
    })
    assert desk.phase is DeskPhase.INIT

    result = _build_v1_compatible_result(desk, elapsed_s=1.0)

    assert result["action"] == "DEGRADED"          # the one honest field
    stages = result["v2_metadata"]["stages_completed"]
    claimed = [s for s in ("research", "debate", "decision") if s in stages]
    assert not claimed, (
        f"a desk stuck at INIT with one agent reports stages_completed={stages} "
        f"— it claims {claimed} ran"
    )
    assert result["v2_metadata"]["debate"]["integrity_status"] != "passed", (
        "a desk that never held a debate reports integrity_status 'passed'"
    )
