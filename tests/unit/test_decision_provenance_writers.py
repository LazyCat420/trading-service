"""Every `DecisionProvenance` value must be produced by something.

WHY
---
`DecisionProvenance` exists to make "an agent decided this" unforgeable: only
`BOARD_REASONED` counts toward accuracy, and `_DEGRADED_PROVENANCE` decides what
gets labelled a pipeline failure. A member with no writer is an advertised
distinction that does not exist in the data — every consumer that branches on it
is dead code, and every reader who sees the enum believes the pipeline records a
difference it never records.

Found 2026-08-10 by sweeping the app for writers:

| value | writers before | after |
|---|---|---|
| `BOARD_REASONED` | agent_runner + orchestrator | unchanged |
| `BOARD_DEGRADED_FALLBACK` | 2 orchestrator sites | unchanged |
| `TRIAGE_SKIP` | 2 orchestrator sites | unchanged |
| `TIMEOUT_ABORT` | **none** — only a membership test in `_DEGRADED_PROVENANCE` | stamped when the board's `PhaseOutcome` is `TIMED_OUT` |
| `NO_TRADE_GATE_SKIP` | **none** | still none — see `_KNOWN_UNWRITTEN` |
| `COERCED_UNSHORTABLE` | `shared_desk` default only | unchanged |
| `UNATTRIBUTED` | `shared_desk` default only | unchanged |

This is a static scan: it reads the source, so it costs nothing and needs no
database, no cycle and no LLM.
"""

from __future__ import annotations

import ast
import os

import pytest

from app.v3.shared_desk import DecisionProvenance

_APP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "app",
)

#: Members with no writer, each with a reason. A member may sit here only while
#: someone has decided the gap is acceptable — it is a written admission, not a
#: silent exemption, and a NEW member cannot join without editing this file.
_KNOWN_UNWRITTEN = {
    # Documented as "unheld + unanimously bearish → debate skipped". That path
    # exists (the desk can only buy, so a unanimous bear case is a HOLD), but it
    # stamps nothing, so a gate-skipped HOLD is currently indistinguishable from
    # a board-reasoned one. Fixing it means finding the skip site and stamping
    # there; it is NOT in `_DEGRADED_PROVENANCE`, so mislabelling it would move
    # healthy skips into the failure count — which is exactly the mistake the
    # `_DEGRADED_PROVENANCE` comment in orchestrator.py warns about.
    DecisionProvenance.NO_TRADE_GATE_SKIP,
}

#: Written by `SharedDesk.append_artifact` itself as defaults, so they never
#: appear at a call site.
_WRITTEN_BY_THE_DESK_DEFAULT = {
    DecisionProvenance.UNATTRIBUTED,
    DecisionProvenance.COERCED_UNSHORTABLE,
}


def _members_referenced_outside_the_enum() -> set[str]:
    """Every `DecisionProvenance.<NAME>` attribute access in app/, by AST.

    Excludes `shared_desk.py`, which declares the enum and applies the two
    defaults — a reference there proves declaration, not production.
    """
    seen: set[str] = set()
    for root, _dirs, files in os.walk(_APP):
        if "__pycache__" in root:
            continue
        for fname in files:
            if not fname.endswith(".py") or fname == "shared_desk.py":
                continue
            path = os.path.join(root, fname)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "DecisionProvenance"
                ):
                    seen.add(node.attr)
    return seen


def test_the_scan_finds_references():
    """Vacuity guard — an empty scan would make every member look unwritten."""
    found = _members_referenced_outside_the_enum()
    assert len(found) >= 3, (
        f"only found {sorted(found)}; the check below would report every member "
        "as unwritten regardless of the truth"
    )


@pytest.mark.parametrize("member", list(DecisionProvenance))
def test_every_provenance_value_is_produced_somewhere(member):
    if member in _WRITTEN_BY_THE_DESK_DEFAULT or member in _KNOWN_UNWRITTEN:
        pytest.skip(f"{member.name}: exempt with a written reason in this file")

    assert member.name in _members_referenced_outside_the_enum(), (
        f"DecisionProvenance.{member.name} has no writer outside shared_desk.py. "
        "An enum value nothing produces is an advertised distinction that does "
        "not exist in the data: every consumer branching on it is dead code. "
        "Either stamp it at the path it describes, or delete it — and if the "
        "gap is deliberate, add it to _KNOWN_UNWRITTEN with the reason."
    )


def test_timeout_abort_is_distinguishable_from_a_generic_degrade():
    """The specific gap closed on 2026-08-10, pinned.

    Both values live in `_DEGRADED_PROVENANCE`, so scoring cannot tell them
    apart by design — the point is that the *diagnosis* can. Asserting only
    "TIMEOUT_ABORT is referenced" would pass on the membership test that already
    existed before there was a writer, so this asserts the write site instead.
    """
    src = open(os.path.join(_APP, "v3", "orchestrator.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    stamped_in_a_dict = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "decision_provenance"):
                continue
            if "TIMEOUT_ABORT" in ast.dump(value):
                stamped_in_a_dict = True

    assert stamped_in_a_dict, (
        "no decision artifact stamps DecisionProvenance.TIMEOUT_ABORT — a board "
        "that timed out is being recorded as a generic BOARD_DEGRADED_FALLBACK, "
        "which is the distinction the enum claims to make"
    )
