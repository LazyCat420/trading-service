"""Component health monitor — verdict thresholds, the 3-strike disable, and
the mode gate in the quant block.

The load-bearing assertions: (1) 'redundant' NEVER auto-disables — that call
was explicitly left to a human on 2026-08-03; (2) an unreadable metrics table
counts as insufficient_data, not failing — a broken monitor must not
accumulate strikes against a working component; (3) modes 1/2 skip the desk
fit entirely rather than paying ~22-32s to render nothing.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.autoresearch import component_health as ch


# ── verdicts (pure) ──────────────────────────────────────────────────

def _healthy_metrics(**overrides):
    m = {
        "observations": 100,
        "coverage": {"ok": True, "passes": True, "direction": None,
                     "p_value": 0.4},
        "vs_free": {"qlike_t": -2.5, "mse_t": -1.0,
                    "qlike_mean_diff": -0.01, "mse_mean_diff": -0.001},
        "operational": {"gap_trading_days": 0, "stale_run": 0},
    }
    m.update(overrides)
    return m


def test_healthy_needs_calibration_and_a_win_over_free():
    verdict, failures = ch.decide_verdict(_healthy_metrics())
    assert verdict == ch.VERDICT_HEALTHY
    assert failures == []


def test_not_beating_free_is_redundant_not_failing():
    m = _healthy_metrics(
        vs_free={"qlike_t": -0.9, "mse_t": 1.5,
                 "qlike_mean_diff": 0.0, "mse_mean_diff": 0.0},
    )
    verdict, _ = ch.decide_verdict(m)
    assert verdict == ch.VERDICT_REDUNDANT


def test_worse_than_free_on_both_losses_is_failing():
    m = _healthy_metrics(
        vs_free={"qlike_t": 2.4, "mse_t": 3.0,
                 "qlike_mean_diff": 0.05, "mse_mean_diff": 0.01},
    )
    verdict, failures = ch.decide_verdict(m)
    assert verdict == ch.VERDICT_FAILING
    assert any("free baseline" in f for f in failures)


def test_worse_on_one_loss_only_is_not_failing():
    """The 2026-08-03 race result exactly: MSE significantly worse, QLIKE
    not. That graded REDUNDANT then and must keep grading REDUNDANT — the
    two losses disagree about over-forecasting, and one alone is not harm."""
    m = _healthy_metrics(
        vs_free={"qlike_t": -0.89, "mse_t": 2.73,
                 "qlike_mean_diff": 0.0, "mse_mean_diff": 0.01},
    )
    verdict, _ = ch.decide_verdict(m)
    assert verdict == ch.VERDICT_REDUNDANT


def test_band_too_narrow_is_failing_but_too_wide_is_not():
    narrow = _healthy_metrics(
        coverage={"ok": True, "passes": False, "direction": "too_narrow",
                  "p_value": 0.001},
    )
    verdict, failures = ch.decide_verdict(narrow)
    assert verdict == ch.VERDICT_FAILING
    assert any("NARROW" in f for f in failures)

    wide = _healthy_metrics(
        coverage={"ok": True, "passes": False, "direction": "too_wide",
                  "p_value": 0.001},
    )
    verdict, _ = ch.decide_verdict(wide)
    assert verdict == ch.VERDICT_REDUNDANT


def test_snapshot_gap_and_stale_run_fail_regardless_of_n():
    """Operational failures must not hide behind insufficient_data: a broken
    snapshot job stops the very data the statistical checks need."""
    m = _healthy_metrics(
        observations=5,
        operational={"gap_trading_days": ch.GAP_TRADING_DAYS_TO_FAIL,
                     "stale_run": 0},
    )
    verdict, failures = ch.decide_verdict(m)
    assert verdict == ch.VERDICT_FAILING
    assert any("gap" in f for f in failures)

    m = _healthy_metrics(
        operational={"gap_trading_days": 0,
                     "stale_run": ch.STALE_RUN_TO_FAIL},
    )
    verdict, failures = ch.decide_verdict(m)
    assert verdict == ch.VERDICT_FAILING
    assert any("stale" in f for f in failures)


def test_small_n_without_operational_failure_is_insufficient():
    verdict, _ = ch.decide_verdict(_healthy_metrics(observations=10))
    assert verdict == ch.VERDICT_INSUFFICIENT


def test_unreadable_metrics_is_insufficient_not_failing():
    verdict, failures = ch.decide_verdict({"error": "db unreachable"})
    assert verdict == ch.VERDICT_INSUFFICIENT
    assert failures and "error" in failures[0]


# ── the 3-strike disable (pure) ──────────────────────────────────────

def test_redundant_never_disables_whatever_the_streak():
    assert ch.decide_action(ch.VERDICT_REDUNDANT, streak=99, current_mode=0) == "none"


def test_failing_below_three_strikes_does_not_disable():
    assert ch.decide_action(ch.VERDICT_FAILING, streak=2, current_mode=0) == "none"


def test_three_failing_strikes_disable_once():
    assert ch.decide_action(ch.VERDICT_FAILING, streak=3, current_mode=0) == "auto_disable"
    # Already withheld (mode 1) or fully off (mode 2): no second action —
    # re-proposing every day would spam runtime_parameters and the user.
    assert ch.decide_action(ch.VERDICT_FAILING, streak=3, current_mode=1) == "already_disabled"
    assert ch.decide_action(ch.VERDICT_FAILING, streak=3, current_mode=2) == "already_disabled"


# ── registry + governor wiring ───────────────────────────────────────

def test_hmm_regime_mode_is_registered_with_sane_envelope():
    from app.services.parameter_store import PARAMETER_REGISTRY

    spec = PARAMETER_REGISTRY["HMM_REGIME_MODE"]
    assert spec.default == 0          # fail-open lands on ACTIVE
    assert spec.min_value == 0 and spec.max_value == 2
    assert spec.kind == "int"


def test_monitor_is_authorized_to_propose():
    from app.validation.parameter_validator import ParameterValidator

    ok, why, change = ParameterValidator.validate_proposal(
        "HMM_REGIME_MODE", 1, agent="component_health_monitor",
        reason="component_health: 3 consecutive failing evaluations (test)",
    )
    assert ok, why
    # Neutral direction -> permanent until a human changes it back, which is
    # the point: the monitor never re-enables, so nothing should auto-expire
    # the withhold under it either.
    assert change.ttl_hours is None


# ── the mode gate in the quant block ─────────────────────────────────

def test_mode_1_skips_the_desk_fit_entirely(monkeypatch):
    from app.quant import context_block, regime_hmm

    monkeypatch.setattr(regime_hmm, "hmm_regime_mode", lambda: 1)

    def _explode(*a, **k):
        raise AssertionError("mode 1 must not fit/render the HMM in the desk path")

    monkeypatch.setattr(regime_hmm, "build_hmm_context_line", _explode)
    block = context_block.build_quant_math_block("SPY", cycle_id="cycle-test")
    assert "HMM regime shadow" not in block


def test_mode_0_still_renders(monkeypatch):
    from app.quant import context_block, regime_hmm

    monkeypatch.setattr(regime_hmm, "hmm_regime_mode", lambda: 0)
    monkeypatch.setattr(
        regime_hmm, "build_hmm_context_line",
        lambda as_of=None, cycle_id=None: "- HMM regime shadow (test line)",
    )
    block = context_block.build_quant_math_block("SPY", cycle_id="cycle-test")
    assert "HMM regime shadow (test line)" in block


def test_mode_read_failure_fails_open_to_active(monkeypatch):
    from app.quant import regime_hmm
    from app.services import parameter_store

    def _boom(key):
        raise RuntimeError("store down")

    monkeypatch.setattr(parameter_store, "get_param", _boom)
    assert regime_hmm.hmm_regime_mode() == 0


# ── the evaluation entrypoint never raises ───────────────────────────

def test_run_evaluation_survives_a_dead_database(monkeypatch):
    monkeypatch.setattr(
        ch, "compute_hmm_metrics",
        lambda ticker="SPY": (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    report = ch.run_component_health_evaluation()
    assert "error" in report  # reported, not raised
