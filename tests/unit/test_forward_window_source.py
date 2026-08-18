"""One-vendor forward windows for the evaluation layer (2026-07-30).

The vendor-mixing bug fixed in the return LOADERS was still live in everything
that SCORES a decision. `price_history` keeps `source` in its primary key, so a
dual-source ticker carries two prints per date, and:

  * `ORDER BY date ASC LIMIT sessions` returns `sessions` ROWS spanning about
    half as many DATES — a "+7 session" move measured over ~3 sessions.
    Measured on CRH: +0.970% where the truth is -2.358%. A SIGN FLIP.
  * `ORDER BY date DESC LIMIT 1` picks between the two vendors
    non-deterministically, so an entry price read once and an exit price read
    again turn a vendor SPREAD (mean 20.05%; ALLY 1.11%, DRIP 718%) into P&L.

Blast radius when found: 146 of 773 completed desks (19%), 20 of 122 tickers —
and `outcome_tracker` writes the persisted `decision_outcomes.pnl_pct` that
`parameter_store` cites to justify the confidence floor.
"""

from unittest.mock import patch

import pytest

from app.quant import returns as R


# ── contract of the helpers ──────────────────────────────────────────

def test_forward_window_refuses_a_short_window():
    """A partial window silently scored as a full one is the whole bug."""
    with _closes(10.0, 11.0):
        assert R.forward_window("X", "2026-07-01", 8) is None


def test_forward_window_returns_exactly_n_closes():
    with _closes(*[float(i) for i in range(1, 9)]):
        w = R.forward_window("X", "2026-07-01", 8)
    assert w is not None and len(w) == 8


def test_forward_move_pct_is_first_to_last():
    with _closes(100.0, 101.0, 110.0):
        assert R.forward_move_pct("X", "2026-07-01", 3) == pytest.approx(10.0)


def test_forward_move_pct_none_when_window_open():
    with _closes(100.0):
        assert R.forward_move_pct("X", "2026-07-01", 5) is None


@pytest.mark.parametrize("bad", [[0.0, 1.0, 2.0], [float("nan"), 1.0, 2.0]])
def test_non_positive_and_nan_closes_are_dropped(bad):
    """NaN survives a NOT NULL check and compares false against every bound."""
    with _closes(*bad):
        assert R.forward_window("X", "2026-07-01", 3) is None


def test_latest_close_rejects_nan_and_zero():
    with _closes(float("nan")):
        assert R.latest_close("X") is None
    with _closes(0.0):
        assert R.latest_close("X") is None
    with _closes(42.5):
        assert R.latest_close("X") == pytest.approx(42.5)


# ── the invariant that actually prevents the bug ─────────────────────

@pytest.mark.parametrize("fn", ["latest_close", "forward_window"])
def test_every_helper_filters_by_source(fn):
    """Pin the filter itself. Without it these are the buggy queries again.

    The assertion is on the Mongo read now, not on the SQL helper's name: these
    functions call `mongo_store.find_docs("price_history", ...)`, and the pin
    can arrive either as a `source` key in the filter (single ticker, one
    dominant vendor) or via `keep_dominant_source()` over the returned frame
    (multi-ticker). Either satisfies the rule; neither being present is the
    2026-07-30 bug back again, in a client the SQL-text guard cannot read.
    """
    import ast
    import inspect
    import textwrap

    src = inspect.getsource(getattr(R, fn))
    if "keep_dominant_source" in src:
        return

    tree = ast.parse(textwrap.dedent(src))
    reads = [
        ast.unparse(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id in ("mongo_store", "mongo_query")
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "price_history"
    ]
    assert reads, f"{fn} no longer reads price_history — has it been renamed?"
    for call in reads:
        assert "'source'" in call or '"source"' in call, (
            f"{fn} must pin ONE vendor; an unfiltered price_history read is "
            f"the bug. Offending read:\n    {call}"
        )


def test_evaluation_paths_do_not_read_price_history_directly():
    """outcome_tracker writes the persisted P&L behind the confidence floor."""
    import inspect
    from app.autoresearch import outcome_tracker

    src = inspect.getsource(outcome_tracker)
    # Strip comment lines — the module explains the bug in prose, and matching
    # the explanation instead of the code is how a guard tests nothing.
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "ORDER BY date DESC LIMIT 1" not in code, (
        "outcome_tracker must use app.quant.returns.latest_close — an "
        "unfiltered latest close picks between vendors non-deterministically"
    )
    assert "latest_close" in code


def test_scorecard_uses_the_canonical_window():
    import inspect
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts", "agent_scorecard.py")
    src = open(path).read()
    assert "forward_move_pct" in src, "scorecard must use the canonical window"
    assert "BENCHMARK_TICKER" in src, "scorecard must report excess over the market"
    assert "NON-OVERLAPPING" in src, (
        "scorecard must state how many independent windows back its rows — "
        "overlapping forward windows in a 5-week span made p=0.001 out of ~2"
    )


# ── helpers ──────────────────────────────────────────────────────────

def _closes(*values):
    """Patch the Mongo read with `price_history` DOCUMENTS.

    `latest_close` and `forward_window` read through `mongo_store.find_docs`
    now, which returns dicts — and they import `mongo_store` INSIDE the
    function body, so patching a module attribute on `app.quant.returns` would
    be a silent no-op. The patch target has to be `app.db.mongo_store.find_docs`
    itself.
    """
    docs = [{"close": v} for v in values]
    return patch("app.db.mongo_store.find_docs", return_value=docs)
