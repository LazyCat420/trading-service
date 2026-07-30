"""The power calculation must be checkable, or it is just another asserted number.

`scripts/power_report.py` exists because the "minimum detectable effect is
2.24pp" figure governing what is worth building could not be reproduced. A
calculator nobody can check has the same problem, so the math is pinned here
against closed-form cases and against the documented figure itself.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.power_report import (  # noqa: E402
    effective_n,
    intraclass_correlation,
    mde,
)


class TestMDE:
    def test_reproduces_the_documented_figure(self):
        """sd=5.0pp at n=157 is where "2.24pp" came from.

        If this drifts, either the formula changed or the number quoted in
        every planning document since 2026-07-30 was never this calculation.
        """
        assert mde(5.0, 157) == pytest.approx(2.24, abs=0.01)

    def test_shrinks_with_the_square_root_of_n(self):
        """Quadrupling the sample halves the detectable effect."""
        assert mde(5.0, 400) == pytest.approx(mde(5.0, 100) / 2, rel=1e-9)

    def test_scales_linearly_with_dispersion(self):
        assert mde(10.0, 157) == pytest.approx(2 * mde(5.0, 157), rel=1e-9)

    def test_undefined_below_two_samples(self):
        assert math.isnan(mde(5.0, 1))
        assert math.isnan(mde(0.0, 157))

    def test_more_power_demands_a_larger_effect(self):
        """Asking for 99% power cannot make a smaller effect detectable."""
        assert mde(5.0, 157, z_power=2.326) > mde(5.0, 157, z_power=0.8416)


class TestIntraclassCorrelation:
    def test_independent_clusters_give_rho_near_zero(self):
        """Negative control. Block means that do not differ carry no clustering."""
        clusters = [[1.0, -1.0, 2.0, -2.0] for _ in range(10)]
        assert intraclass_correlation(clusters) == pytest.approx(0.0, abs=0.05)

    def test_constant_within_cluster_gives_rho_one(self):
        """Every desk in a window sharing an outcome = one real observation."""
        clusters = [[float(i)] * 5 for i in range(10)]
        assert intraclass_correlation(clusters) == pytest.approx(1.0, abs=1e-6)

    def test_undefined_with_a_single_cluster(self):
        assert math.isnan(intraclass_correlation([[1.0, 2.0, 3.0]]))


class TestEffectiveN:
    def test_rho_zero_keeps_the_whole_sample(self):
        clusters = [[1.0] * 10 for _ in range(5)]
        assert effective_n(clusters, 0.0) == pytest.approx(50.0)

    def test_rho_one_collapses_each_cluster_to_one_observation(self):
        clusters = [[1.0] * 10 for _ in range(5)]
        assert effective_n(clusters, 1.0) == pytest.approx(5.0)

    def test_partial_correlation_lands_between(self):
        clusters = [[1.0] * 10 for _ in range(5)]
        n_eff = effective_n(clusters, 0.5)
        assert 5.0 < n_eff < 50.0

    def test_the_naive_count_always_overstates(self):
        """The bug this whole script exists to prevent: treating 1,785
        clustered decisions as 1,785 independent draws."""
        clusters = [[float(j) for j in range(20)] for _ in range(8)]
        n_raw = sum(len(c) for c in clusters)
        assert effective_n(clusters, 0.2) < n_raw


def test_a_clustered_sample_yields_a_worse_ceiling_than_the_naive_one():
    """End-to-end: the correction must move the ceiling the pessimistic way.

    A calculator that made the constraint look easier to clear would be worse
    than none at all.
    """
    clusters = [[float(j % 7) for j in range(200)] for _ in range(8)]
    n_raw = sum(len(c) for c in clusters)
    rho = intraclass_correlation(clusters)
    n_eff = effective_n(clusters, max(rho, 0.05))
    assert mde(28.0, n_eff) > mde(28.0, n_raw)
