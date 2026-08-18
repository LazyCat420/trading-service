"""Every literal artifact type in the source must exist in `_VALID_ARTIFACT_TYPES`.

THE BUG THIS WOULD HAVE CAUGHT, STATICALLY
------------------------------------------
`SharedDesk.append_artifact` raises `ValueError` for an unknown artifact_type.
`orchestrator.py` called it with `"degradation_note"`, which was never in the
set. The raise propagated into a blind `except Exception` upstream, so the
whole triage-override branch — and every analyst dispatch below it — became
dead code with nothing logged. 22 attempts between 2026-07-28 and 2026-08-10,
0 completions; the tickers ended at `INIT` while a decision row claimed a full
pipeline had run.

The 2026-08-10 fix added `"degradation_note"` to the set. That closed the
instance, not the class: the next call site that invents a type gets exactly
the same silent death.

WHY A STATIC SCAN AND NOT AN EXCEPT-REMOVAL
-------------------------------------------
There are 1,146 `BLE001` blind-excepts and 188 `try-except-pass` blocks in
`app/`. Removing them wholesale is an unbounded change with real risk — many
are correct fail-open observers. This test attacks the same failure from the
other end: it makes the mismatch impossible to introduce, so no except needs to
be trusted to report it. It runs in milliseconds and needs no database, no
network and no cycle.

It reads the AST rather than grepping, so it anchors on the call node that owns
the argument instead of on a nearby line.
"""

from __future__ import annotations

import ast
import os

import pytest

_APP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "app",
)


def _literal_call_sites() -> list[tuple[str, int, str]]:
    """(relative path, line, artifact_type) for every literal append_artifact call."""
    found: list[tuple[str, int, str]] = []
    for root, _dirs, files in os.walk(_APP):
        if "__pycache__" in root:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            except SyntaxError:
                continue
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
                    found.append((os.path.relpath(path, _APP), node.lineno, first.value))
    return found


def test_the_scan_finds_call_sites():
    """Vacuity guard — a scan that matches nothing passes the check below."""
    sites = _literal_call_sites()
    assert len(sites) >= 10, (
        f"only found {len(sites)} append_artifact call sites; the check below "
        "proved nothing (did the method get renamed?)"
    )


def test_every_literal_artifact_type_is_registered():
    from app.v3.shared_desk import _VALID_ARTIFACT_TYPES

    unknown = [
        (path, line, kind)
        for path, line, kind in _literal_call_sites()
        if kind not in _VALID_ARTIFACT_TYPES
    ]
    assert not unknown, (
        "These call sites pass an artifact_type that append_artifact will "
        "reject with ValueError. Upstream blind excepts swallow that raise, so "
        "the branch — and everything dispatched below it — becomes dead code "
        "with nothing logged:\n"
        + "\n".join(f"  app/{p}:{ln}  {k!r}" for p, ln, k in unknown)
        + "\n\nAdd the type to _VALID_ARTIFACT_TYPES in app/v3/shared_desk.py, "
        "and check app/agents/whiteboard_sections.py — a new type becomes "
        "DESK_CARRIED there automatically."
    )


@pytest.mark.parametrize(
    "kind",
    sorted(
        {k for _p, _l, k in _literal_call_sites()}
    ),
)
def test_each_used_type_round_trips_through_append_artifact(kind):
    """The set membership is necessary; this proves the call actually succeeds.

    A type could be in the frozenset and still be rejected by a later guard
    (provenance stamping, decision-artifact rules). Checking membership alone
    would be a check that passes in both states for that second failure mode.
    """
    from app.v3.shared_desk import SharedDesk

    desk = SharedDesk(cycle_id="test-artifact-types", ticker="TEST")
    desk.append_artifact(kind, {"summary": "round-trip probe"})

    # `append_artifact` ends in `setattr(self, artifact_type, artifact)` — the
    # artifact type IS the attribute name. There is no `desk.artifacts` list.
    landed = getattr(desk, kind, None)
    assert isinstance(landed, dict) and landed.get("_artifact_type") == kind, (
        f"{kind!r} was accepted by append_artifact but did not land on "
        f"desk.{kind} (got {landed!r})"
    )
