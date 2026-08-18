"""Kupiec proportion-of-failures — the missing self-validating control.

`scripts/power_report.py` names a coverage test as the way out of this desk's
measurement wall (MDE 8.84pp on 329 effective decisions) and no implementation
existed. A forecast band is settled on the model's OWN daily output, where n is
thousands, instead of on downstream P&L, where it is hundreds.

These tests are built as CONTROLS, not smoke checks: a correctly-calibrated
band must PASS, and a miscalibrated one must FAIL in the right DIRECTION, with
the failure getting stronger as the miscalibration grows. A gate that passes
everything and a gate that fails everything are equally useless.
"""

import math

import numpy as np
import pytest

from app.quant.stat_gates import MIN_OBSERVATIONS, coverage_gate, kupiec_pof


# ── the test discriminates ───────────────────────────────────────────

def test_exactly_calibrated_band_passes():
    # 50 breaches in 1000 at a stated 5% — the null is exactly true.
    r = kupiec_pof(breaches=50, observations=1000, expected_rate=0.05)
    assert r["ok"] and r["passes"]
    assert r["direction"] == "calibrated"
    assert r["lr_statistic"] == pytest.approx(0.0, abs=1e-9)
    assert r["p_value"] > 0.99


def test_too_many_breaches_fails_as_too_narrow():
    """The dangerous direction: the model understates risk."""
    r = kupiec_pof(breaches=150, observations=1000, expected_rate=0.05)
    assert r["ok"] and not r["passes"]
    assert r["direction"] == "too_narrow"
    assert r["p_value"] < 0.001


def test_too_few_breaches_fails_as_too_wide():
    """An always-huge band is never breached and forecasts nothing. It must
    fail too — this is the half of the test a one-sided check would miss."""
    r = kupiec_pof(breaches=2, observations=1000, expected_rate=0.05)
    assert r["ok"] and not r["passes"]
    assert r["direction"] == "too_wide"
    assert r["p_value"] < 0.001


def test_the_statistic_scales_with_the_miscalibration():
    """A positive control must SCALE the effect, not just trip a threshold.

    Holding n fixed and walking the breach rate away from the stated 5%, the
    LR statistic must increase monotonically. If it did not, a "significant"
    verdict would carry no information about how wrong the model is.
    """
    stats = [
        kupiec_pof(breaches=b, observations=1000, expected_rate=0.05)["lr_statistic"]
        for b in (50, 70, 90, 120, 160)
    ]
    assert stats == sorted(stats), stats
    assert stats[-1] > stats[0] + 50


def test_more_data_makes_the_same_bias_more_significant():
    """Same 10% observed rate against a stated 5%, at n=100 vs n=2000: the
    larger sample must be more damning. This is what separates a real test
    from a fixed threshold on a ratio."""
    small = kupiec_pof(breaches=10, observations=100, expected_rate=0.05)
    large = kupiec_pof(breaches=200, observations=2000, expected_rate=0.05)
    assert small["observed_rate"] == large["observed_rate"] == 0.10
    assert large["lr_statistic"] > small["lr_statistic"]
    assert large["p_value"] < small["p_value"]


# ── boundaries and refusals ──────────────────────────────────────────

def test_zero_breaches_does_not_produce_nan():
    """0*log(0) is the obvious way this blows up."""
    r = kupiec_pof(breaches=0, observations=500, expected_rate=0.05)
    assert r["ok"]
    assert not math.isnan(r["lr_statistic"])
    assert not math.isnan(r["p_value"])
    assert r["direction"] == "too_wide"


def test_all_breaches_does_not_produce_nan():
    r = kupiec_pof(breaches=500, observations=500, expected_rate=0.05)
    assert r["ok"]
    assert not math.isnan(r["lr_statistic"])
    assert r["direction"] == "too_narrow"


def test_too_little_data_is_not_a_pass():
    """"Could not check" must never read as "checked and fine"."""
    r = kupiec_pof(breaches=1, observations=MIN_OBSERVATIONS - 1, expected_rate=0.05)
    assert not r["ok"]
    assert "passes" not in r or not r["passes"]


@pytest.mark.parametrize("rate", [0.0, 1.0, -0.1, 1.5])
def test_invalid_expected_rate_refused(rate):
    assert not kupiec_pof(5, 100, rate)["ok"]


def test_breaches_above_n_refused():
    assert not kupiec_pof(101, 100, 0.05)["ok"]


# ── coverage_gate: the array-facing wrapper ──────────────────────────

def test_coverage_gate_on_an_exactly_calibrated_band():
    """Deterministic control: exactly 5% of observations breach.

    Deliberately NOT a random draw. The first version of this test sampled
    4000 N(0,1) and asserted the honest band passes — it drew 166 breaches
    (4.15%), a 2.5-sigma fluctuation, and failed. That is the test working as
    designed: at alpha=0.05 an honest band is rejected on ~5% of seeds, so a
    single-draw calibration control is flaky BY CONSTRUCTION. The size of the
    test is checked properly in the next test; this one checks the arithmetic.
    """
    # Uniform 0.000 .. 1.999 so the band actually bites as it moves: values
    # bunched far below it would keep the breach count flat while the band
    # narrowed, which is how the first draft of this test fooled itself.
    n = 2000
    r = np.arange(n) / 1000.0
    b = np.full(n, 1.899)        # exceeded by 1.900..1.999 -> exactly 100

    out = coverage_gate(r, b, expected_rate=0.05, label="exact")
    assert out["ok"] and out["passes"], out
    assert out["breaches"] == 100
    assert out["observed_rate"] == 0.05
    assert out["direction"] == "calibrated"

    narrow = coverage_gate(r, np.full(n, 0.949), expected_rate=0.05, label="narrow")
    assert narrow["breaches"] == 1050
    assert not narrow["passes"] and narrow["direction"] == "too_narrow"

    wide = coverage_gate(r, np.full(n, 5.0), expected_rate=0.05, label="wide")
    assert wide["breaches"] == 0
    assert not wide["passes"] and wide["direction"] == "too_wide"


def test_the_gate_has_roughly_the_right_false_positive_rate():
    """Size check: an honest band must be rejected ~5% of the time, not 0%
    and not 50%.

    This is the property that makes the gate trustworthy, and it can only be
    seen across many draws. A gate that never rejects an honest model is
    useless in the other direction — it would pass anything.
    """
    rejects = 0
    trials = 300
    for seed in range(trials):
        rng = np.random.default_rng(seed)
        r = rng.standard_normal(1000)
        out = coverage_gate(r, np.full(1000, 1.959964), expected_rate=0.05)
        rejects += (not out["passes"])

    rate = rejects / trials
    assert 0.01 <= rate <= 0.12, (
        f"false-positive rate {rate:.3f} over {trials} honest draws — the gate "
        f"should reject a correctly-specified band about 5% of the time"
    )


def test_coverage_gate_scales_with_a_vol_misestimate():
    """A model that understates volatility by k× must look progressively
    worse as k grows — the property that makes this usable for grading a
    regime model's vol claim."""
    rng = np.random.default_rng(7)
    r = rng.standard_normal(4000)
    band = np.full(4000, 1.959964)
    stats = [
        coverage_gate(r, band / k, expected_rate=0.05)["lr_statistic"]
        for k in (1.0, 1.25, 1.5, 2.0)
    ]
    assert stats == sorted(stats), stats


def test_length_mismatch_refused():
    assert not coverage_gate([1, 2, 3], [1, 2], 0.05)["ok"]


def test_non_finite_pairs_are_dropped_not_counted():
    r = [0.5, float("nan"), 0.2, 3.0]
    b = [1.0, 1.0, float("inf"), 1.0]
    out = coverage_gate(r, b, expected_rate=0.05)
    # Only 2 usable pairs -> below MIN_OBSERVATIONS, so it refuses rather
    # than reporting a breach rate computed from two points.
    assert not out["ok"]
    assert out.get("dropped_pairs") == 2


def test_zero_or_negative_bands_are_dropped():
    """A band of 0 would count every observation as a breach and silently
    turn a data bug into a 100% failure rate."""
    n = 60
    r = np.full(n, 0.1)
    b = np.full(n, 1.0)
    b[:10] = 0.0
    out = coverage_gate(r, b, expected_rate=0.05)
    assert out["ok"]
    assert out["dropped_pairs"] == 10
    assert out["observations"] == n - 10
    assert out["breaches"] == 0
