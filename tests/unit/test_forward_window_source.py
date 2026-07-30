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

import pytest

from app.quant import returns as R


# ── contract of the helpers ──────────────────────────────────────────

def test_forward_window_refuses_a_short_window(monkeypatch):
    """A partial window silently scored as a full one is the whole bug."""
    monkeypatch.setattr(R, "get_db", _fake_db([(10.0,), (11.0,)]))
    assert R.forward_window("X", "2026-07-01", 8) is None


def test_forward_window_returns_exactly_n_closes(monkeypatch):
    monkeypatch.setattr(R, "get_db", _fake_db([(float(i),) for i in range(1, 9)]))
    w = R.forward_window("X", "2026-07-01", 8)
    assert w is not None and len(w) == 8


def test_forward_move_pct_is_first_to_last(monkeypatch):
    monkeypatch.setattr(R, "get_db", _fake_db([(100.0,), (101.0,), (110.0,)]))
    assert R.forward_move_pct("X", "2026-07-01", 3) == pytest.approx(10.0)


def test_forward_move_pct_none_when_window_open(monkeypatch):
    monkeypatch.setattr(R, "get_db", _fake_db([(100.0,)]))
    assert R.forward_move_pct("X", "2026-07-01", 5) is None


@pytest.mark.parametrize("bad", [[(0.0,), (1.0,), (2.0,)], [(float("nan"),), (1.0,), (2.0,)]])
def test_non_positive_and_nan_closes_are_dropped(monkeypatch, bad):
    """NaN survives a NOT NULL check and compares false against every bound."""
    monkeypatch.setattr(R, "get_db", _fake_db(bad))
    assert R.forward_window("X", "2026-07-01", 3) is None


def test_latest_close_rejects_nan_and_zero(monkeypatch):
    monkeypatch.setattr(R, "get_db", _fake_db([(float("nan"),)], one=True))
    assert R.latest_close("X") is None
    monkeypatch.setattr(R, "get_db", _fake_db([(0.0,)], one=True))
    assert R.latest_close("X") is None
    monkeypatch.setattr(R, "get_db", _fake_db([(42.5,)], one=True))
    assert R.latest_close("X") == pytest.approx(42.5)


# ── the invariant that actually prevents the bug ─────────────────────

@pytest.mark.parametrize("fn", ["latest_close", "forward_window"])
def test_every_helper_filters_by_source(fn):
    """Pin the filter itself. Without it these are the buggy queries again."""
    import inspect

    src = inspect.getsource(getattr(R, fn))
    assert "_dominant_source_sql()" in src, (
        f"{fn} must pin ONE vendor; an unfiltered price_history read is the bug"
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

def _fake_db(rows, one=False):
    class _Cur:
        def fetchall(self): return rows
        def fetchone(self): return rows[0] if rows else None

    class _DB:
        def execute(self, *a, **kw): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    return lambda: _DB()
