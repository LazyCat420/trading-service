"""The regime overlay harness — the time-series shape factor_backtest lacks.

Two properties carry this file:

  * the overlay is scored on the INCREMENTAL series, never its own returns.
    Scoring raw overlay returns measures the market: an always-long strategy
    in a rising market passes any "is the mean positive" test, so a broken
    overlay would look like skill.
  * exposure for session t+1 comes from the posterior fitted through t. An
    off-by-one here lets the rule trade a day it has already seen, which
    manufactures an edge out of nothing.
"""

import numpy as np
import pytest

from scripts.regime_overlay_backtest import (
    COST_BPS_PER_SIDE,
    PRIMARY_THRESHOLD,
    _max_drawdown_pct,
    simulate,
)


def _rows(pairs):
    """pairs: (p_stressed, next_return_pct)"""
    return [
        {"as_of": f"2026-01-{i + 1:02d}", "p_stressed": p,
         "next_return_pct": r, "regime": "STRESSED" if p >= 0.5 else "CALM"}
        for i, (p, r) in enumerate(pairs)
    ]


# ── the overlay acts on the right days ───────────────────────────────

def test_exposure_is_cut_only_above_the_threshold():
    rows = _rows([(0.10, 1.0), (0.60, -2.0), (0.20, 1.0), (0.90, -3.0)])
    sim = simulate(rows, PRIMARY_THRESHOLD)
    assert sim["days_out_of_market"] == 2
    # Sat out both losing days, so the overlay beat buy-and-hold here.
    assert sim["incremental"][1] > 0
    assert sim["incremental"][3] > 0


def test_a_fully_invested_overlay_has_a_zero_incremental_series():
    """When the rule never fires there is nothing to test — the harness must
    show exactly zero added value, not a small number from a cost bug."""
    rows = _rows([(0.0, 1.0), (0.1, -1.0), (0.2, 0.5)])
    sim = simulate(rows, PRIMARY_THRESHOLD)
    assert sim["days_out_of_market"] == 0
    assert np.allclose(sim["incremental"], 0.0)
    assert np.allclose(sim["overlay"], sim["baseline"])


def test_sitting_out_a_gain_is_a_negative_increment():
    """The rule must be able to LOSE. A harness where the overlay only ever
    gains is measuring something other than the rule."""
    rows = _rows([(0.90, +4.0)])
    sim = simulate(rows, PRIMARY_THRESHOLD)
    assert sim["incremental"][0] < 0


# ── costs ────────────────────────────────────────────────────────────

def test_switching_costs_are_charged_on_every_change():
    rows = _rows([(0.9, 0.0), (0.1, 0.0), (0.9, 0.0)])
    sim = simulate(rows, PRIMARY_THRESHOLD)
    # Baseline is always invested, so the first day IS a change (1 -> 0),
    # then 0 -> 1, then 1 -> 0: three switches.
    assert sim["switches"] == 3
    expected = -3 * (COST_BPS_PER_SIDE / 100.0)
    assert sim["overlay"].sum() == pytest.approx(expected)


def test_the_first_move_away_from_baseline_is_not_free():
    """Charging only on re-entry would make the initial exit free and quietly
    subsidise a rule that mostly sits out."""
    sim = simulate(_rows([(0.9, 0.0)]), PRIMARY_THRESHOLD)
    assert sim["switches"] == 1
    assert sim["overlay"][0] == pytest.approx(-COST_BPS_PER_SIDE / 100.0)


def test_costs_can_turn_a_break_even_rule_negative():
    """A rule that dodges nothing but trades a lot must show a NEGATIVE
    increment — the honest outcome for churn."""
    rows = _rows([(0.9, 0.0), (0.1, 0.0)] * 20)
    sim = simulate(rows, PRIMARY_THRESHOLD)
    assert float(np.mean(sim["incremental"])) < 0


# ── threshold behaviour ──────────────────────────────────────────────

def test_a_lower_threshold_never_reduces_days_out_of_market():
    rows = _rows([(0.15, 1.0), (0.35, -1.0), (0.55, -2.0), (0.75, -3.0)])
    counts = [simulate(rows, t)["days_out_of_market"] for t in (0.7, 0.5, 0.3, 0.1)]
    assert counts == sorted(counts), counts


# ── point-in-time alignment ──────────────────────────────────────────

def test_alignment_pairs_a_posterior_with_the_NEXT_session(monkeypatch):
    """The posterior for date D may only trade D+1's return. If this paired D
    with D's own return the overlay would be acting on information it did not
    have, and every result downstream would be fiction."""
    import scripts.regime_overlay_backtest as mod
    from datetime import date

    prices = [(date(2026, 1, 5), 100.0), (date(2026, 1, 6), 110.0),
              (date(2026, 1, 7), 99.0)]
    posts = [(date(2026, 1, 5), {"CALM": 0.2, "STRESSED": 0.8}, "STRESSED")]

    class _DB:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params=None):
            self._rows = posts if "regime_hmm_posteriors" in sql else prices
            return self

        def fetchall(self):
            return self._rows

    # load_aligned_series imports both of these inside the function body, so
    # patching them at their source modules is what reaches it.
    import scripts.migration.pg_connection as conn
    import app.quant.returns as rets
    monkeypatch.setattr(conn, "get_db", lambda: _DB())
    monkeypatch.setattr(rets, "dominant_source_sql", lambda: "'yfinance'")

    rows = mod.load_aligned_series("SPY")
    assert len(rows) == 1
    # 01-05's posterior trades 01-06's return: 100 -> 110 = +10%.
    # Pairing it with 01-05's own return would be look-ahead.
    assert rows[0]["next_return_pct"] == pytest.approx(10.0)
    assert rows[0]["p_stressed"] == pytest.approx(0.8)


def test_a_posterior_with_no_following_session_is_dropped(monkeypatch):
    """The newest posterior has nothing to trade yet — including it with a
    zero return would dilute every statistic in the report."""
    import scripts.regime_overlay_backtest as mod
    from datetime import date

    prices = [(date(2026, 1, 5), 100.0)]
    posts = [(date(2026, 1, 5), {"STRESSED": 0.9}, "STRESSED")]

    class _DB:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params=None):
            self._rows = posts if "regime_hmm_posteriors" in sql else prices
            return self

        def fetchall(self):
            return self._rows

    import scripts.migration.pg_connection as conn
    import app.quant.returns as rets
    monkeypatch.setattr(conn, "get_db", lambda: _DB())
    monkeypatch.setattr(rets, "dominant_source_sql", lambda: "'yfinance'")

    assert mod.load_aligned_series("SPY") == []


# ── drawdown helper ──────────────────────────────────────────────────

def test_max_drawdown_is_negative_and_finds_the_trough():
    dd = _max_drawdown_pct(np.array([10.0, -20.0, 5.0]))
    assert dd < 0
    # 1.10 -> 0.88 is a 20% fall from the peak.
    assert dd == pytest.approx(-20.0, abs=1e-6)


def test_max_drawdown_of_a_monotonic_riser_is_zero():
    assert _max_drawdown_pct(np.array([1.0, 1.0, 1.0])) == pytest.approx(0.0)
