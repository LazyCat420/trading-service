"""The gatekeeper may only pick from the pool it was shown. Fail-closed.

The gatekeeper is a SUBSETTER: handed a ranked candidate pool, it chooses from
it. A symbol it names that was not in that pool was invented, and an invented
symbol is not a harmless typo — it is resolved, collected, analysed, decided
on and traded, against a company nobody selected.

Two things were wrong with the inline check this replaces:

  1. `if selected and all_pool:` skipped the check entirely when the pool was
     empty — the state in which the model has the LEAST grounding, since it was
     shown nothing and so anything it names is invented by construction.
  2. `all_pool` was bound only inside `try:` + `with get_db()`, ~470 lines
     above the read.

Neither was reachable in the caller as written, and the test at the bottom
records exactly why, because that is the fact that decays: the coupling which
makes them safe is 400 lines long and invisible from either end.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.services.pipeline_service import admit_gatekeeper_selection

POOL = {"AAPL": {}, "MSFT": {}, "RDDT": {}}


def test_picks_inside_the_pool_are_admitted():
    kept, dropped = admit_gatekeeper_selection(["AAPL", "RDDT"], POOL)
    assert kept == ["AAPL", "RDDT"]
    assert dropped == []


def test_a_pick_outside_the_pool_is_dropped():
    kept, dropped = admit_gatekeeper_selection(["AAPL", "ENRON"], POOL)
    assert kept == ["AAPL"]
    assert dropped == ["ENRON"]


def test_an_empty_pool_admits_nothing():
    """The fail-open this replaces. An empty pool means admit NOTHING."""
    kept, dropped = admit_gatekeeper_selection(["AAPL", "MSFT"], {})
    assert kept == []
    assert dropped == ["AAPL", "MSFT"]


def test_a_missing_pool_admits_nothing():
    assert admit_gatekeeper_selection(["AAPL"], None) == ([], ["AAPL"])


def test_no_picks_is_not_an_error():
    assert admit_gatekeeper_selection([], POOL) == ([], [])
    assert admit_gatekeeper_selection(None, POOL) == ([], [])


def test_order_is_preserved():
    """The pool is RANKED; the gatekeeper's order is its preference."""
    kept, _ = admit_gatekeeper_selection(["RDDT", "AAPL"], POOL)
    assert kept == ["RDDT", "AAPL"]


def test_blank_entries_are_discarded_not_admitted():
    kept, dropped = admit_gatekeeper_selection(["", None, "AAPL"], POOL)
    assert kept == ["AAPL"]
    assert dropped == []


def test_the_drop_is_logged_with_the_symbols(caplog):
    """A silent drop reads as "the gatekeeper only wanted one ticker"."""
    import logging

    with caplog.at_level(logging.WARNING):
        admit_gatekeeper_selection(["AAPL", "ENRON", "WORLDCOM"], POOL)
    assert "ENRON" in caplog.text and "WORLDCOM" in caplog.text


# ── the structural half ──────────────────────────────────────────────────

_SRC = pathlib.Path(__file__).resolve().parents[2] / "app" / "services" / "pipeline_service.py"


def _pipeline_tree() -> ast.Module:
    return ast.parse(_SRC.read_text(encoding="utf-8"))


def _all_pool_sites() -> tuple[list[tuple[int, set[int]]], list[tuple[int, set[int]]]]:
    """Every read and write of `all_pool`, each with the blocks enclosing it."""
    stores: list[tuple[int, set[int]]] = []
    loads: list[tuple[int, set[int]]] = []
    blocks = (ast.Try, ast.With, ast.AsyncWith, ast.If, ast.For, ast.AsyncFor, ast.While)

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[ast.AST] = []

        def generic_visit(self, node):
            self.stack.append(node)
            super().generic_visit(node)
            self.stack.pop()

        def visit_Name(self, node):
            if node.id != "all_pool":
                return
            enclosing = {id(n) for n in self.stack if isinstance(n, blocks)}
            (stores if isinstance(node.ctx, ast.Store) else loads).append(
                (node.lineno, enclosing)
            )

    V().visit(_pipeline_tree())
    return stores, loads


def test_every_read_of_all_pool_is_covered_by_a_binding_that_cannot_be_skipped():
    """The binding must sit no deeper than the reads it serves.

    `all_pool` was assigned only inside `try:` + `with get_db()`, and read ~470
    lines later from outside both. Any exception before the assignment left the
    name unbound, and `if selected and all_pool:` would then raise
    `UnboundLocalError` at the exact moment the gatekeeper's picks were being
    admitted — caught by the enclosing handler as "Portfolio screener failed,
    falling back to AAPL", so a SUCCESSFUL gatekeeper run that chose nine
    tickers is discarded under a log line blaming the screener.

    Asserted as containment, not as "no enclosing blocks at all": this whole
    function body sits inside two cycle-level `try`s, and a test that demanded
    zero enclosing blocks would be unsatisfiable.
    """
    stores, loads = _all_pool_sites()
    assert stores and loads

    first_line, first_blocks = min(stores, key=lambda s: s[0])
    uncovered = [
        line for line, blocks in loads
        if line > first_line and not first_blocks.issubset(blocks)
    ]
    assert not uncovered, (
        f"the first binding of all_pool (line {first_line}) sits inside blocks "
        f"that do not enclose the read(s) at {uncovered} — those reads can run "
        "with the name unbound"
    )


def test_the_admission_call_does_not_gate_on_the_pool_being_truthy():
    """Pin the fail-closed shape at the CALL SITE, not just in the helper.

    Re-introducing `if selected and all_pool:` around the call would restore
    the fail-open while every behavioural test above still passed. Read from
    the AST rather than the text, so a comment that quotes the old form — and
    the comment explaining why it was wrong is worth keeping — does not fail
    the test.
    """
    tree = _pipeline_tree()

    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "admit_gatekeeper_selection"
    ]
    assert calls, "pipeline_service no longer admits gatekeeper picks through the guard"

    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.BoolOp):
            continue
        if not isinstance(node.test.op, ast.And):
            continue
        names = {v.id for v in node.test.values if isinstance(v, ast.Name)}
        assert "all_pool" not in names, (
            f"line {node.lineno}: gating on `all_pool` being truthy skips the "
            "check exactly when the pool is empty — the state in which every "
            "pick the model names is invented by construction"
        )
