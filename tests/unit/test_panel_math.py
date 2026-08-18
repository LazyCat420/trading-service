"""Pooling and scoring for the probabilistic panel.

The tournament's aggregation was a majority vote buried inside a 790-line async
function, so it was never tested against a known answer. This is the whole
reason ``panel_math`` is a separate, I/O-free module: the part that decides what
the panel SAYS must be checkable without a model in the loop.
"""

from __future__ import annotations

import math
import random

import pytest

from app.cognition.debate.panel_math import (
    brier_decomposition,
    brier_score,
    clamp_probability,
    disagreement,
    is_neutral,
    logit,
    pool_probabilities,
    probability_to_action,
    probability_to_confidence,
    sigmoid,
)


class TestClamping:
    def test_nan_resolves_to_no_view(self):
        """NaN must not reach a logit. Every comparison against NaN is False, so
        a naive min/max clamp passes it through — that exact shape (NaN sailing
        through a `<` gate) already broke the confidence floor once."""
        assert clamp_probability(float("nan")) == 0.5
        assert clamp_probability(float("inf")) == 0.5
        assert clamp_probability(float("-inf")) == 0.5

    @pytest.mark.parametrize("bad", [None, {}, [], object(), "high", ""])
    def test_non_numeric_resolves_to_no_view(self, bad):
        """These come from a model's JSON. Never raise inside a debate."""
        assert clamp_probability(bad) == 0.5

    def test_numeric_strings_are_accepted(self):
        """A model that emits "0.7" instead of 0.7 meant 0.7. Coercing it to
        neutral would silently discard a real forecast — the more dangerous
        failure, since it looks like an abstention rather than an error."""
        assert clamp_probability("0.7") == pytest.approx(0.7)
        assert clamp_probability("1") == pytest.approx(0.995)

    def test_zero_and_one_are_pulled_inside(self):
        """p=0/1 map to +-inf, which would let one overconfident agent take the
        pool outright and make the Brier decomposition undefined."""
        assert 0.0 < clamp_probability(0.0) < 0.01
        assert 0.99 < clamp_probability(1.0) < 1.0
        assert math.isfinite(logit(0.0)) and math.isfinite(logit(1.0))

    def test_sigmoid_logit_round_trip(self):
        for p in (0.02, 0.25, 0.5, 0.75, 0.98):
            assert sigmoid(logit(p)) == pytest.approx(p, abs=1e-9)

    def test_sigmoid_is_stable_at_extremes(self):
        """The naive 1/(1+exp(-x)) overflows for large negative x."""
        assert sigmoid(-800) == pytest.approx(0.0, abs=1e-12)
        assert sigmoid(800) == pytest.approx(1.0, abs=1e-12)


class TestPooling:
    def test_unanimous_panel_keeps_its_probability(self):
        assert pool_probabilities([0.9, 0.9, 0.9]) == pytest.approx(0.9, abs=1e-6)

    def test_all_neutral_returns_neutral_not_a_divide_by_zero(self):
        assert pool_probabilities([0.5] * 4) == pytest.approx(0.5, abs=1e-9)

    def test_symmetric_disagreement_cancels(self):
        assert pool_probabilities([0.1, 0.9]) == pytest.approx(0.5, abs=1e-9)

    def test_empty_panel_is_no_view(self):
        assert pool_probabilities([]) == 0.5

    def test_one_confident_agent_does_not_become_the_panel(self):
        """The defect in the naive `w = |logit(p)|` form: a neutral agent gets
        weight ZERO, so [0.95, 0.5, 0.5, 0.5] pools to exactly 0.95 and three
        abstentions read identically to three agreements. An agent saying "I
        don't know" is evidence the call is hard, not an endorsement."""
        pooled = pool_probabilities([0.95, 0.5, 0.5, 0.5])
        assert pooled < 0.95, "one agent must not dictate the panel"
        assert pooled > 0.5, "but a confident view must still lead"

    def test_weighting_is_direction_symmetric(self):
        bull = pool_probabilities([0.95, 0.5, 0.5, 0.5])
        bear = pool_probabilities([0.05, 0.5, 0.5, 0.5])
        assert bull + bear == pytest.approx(1.0, abs=1e-9)

    def test_confidence_weighting_beats_uniform_on_confident_dissent(self):
        """The whole reason for logit weighting: averaging probabilities
        directly under-weights a confident minority."""
        probs = [0.95, 0.5, 0.5, 0.5]
        assert pool_probabilities(probs) > pool_probabilities(
            probs, confidence_weighted=False)

    def test_uniform_control_is_available(self):
        """The rho-style control that shows whether weighting earned anything."""
        probs = [0.8, 0.6, 0.55, 0.51]
        assert pool_probabilities(probs, confidence_weighted=False) != \
            pool_probabilities(probs)

    def test_garbage_in_a_panel_does_not_poison_the_pool(self):
        """One agent returning nonsense degrades to neutral, not to NaN."""
        pooled = pool_probabilities([0.8, float("nan"), None, 0.75])
        assert math.isfinite(pooled)
        assert pooled > 0.5


class TestDisagreement:
    def test_spread_is_reported(self):
        """The number the tournament threw away: a pooled 0.55 from
        [0.54,0.55,0.56] and one from [0.15,0.95,0.55] mean different things."""
        assert disagreement([0.54, 0.55, 0.56]) == pytest.approx(0.02, abs=1e-9)
        assert disagreement([0.15, 0.95, 0.55]) == pytest.approx(0.80, abs=1e-9)

    def test_single_agent_has_no_disagreement(self):
        assert disagreement([0.7]) == 0.0
        assert disagreement([]) == 0.0


class TestActionMapping:
    @pytest.mark.parametrize("p,expected", [
        (0.95, "BUY"), (0.61, "BUY"), (0.55, "HOLD"),
        (0.5, "HOLD"), (0.45, "HOLD"), (0.39, "SELL"), (0.05, "SELL"),
    ])
    def test_probability_maps_to_the_existing_vocabulary(self, p, expected):
        assert probability_to_action(p) == expected

    def test_confidence_is_distance_from_the_coin_flip(self):
        """Downstream `confidence` has always meant "how sure", not "how
        bullish" — so 0.05 and 0.95 must both be high confidence."""
        assert probability_to_confidence(0.5) == 0
        assert probability_to_confidence(0.95) == 90
        assert probability_to_confidence(0.05) == 90
        assert probability_to_confidence(0.75) == 50

    def test_neutral_band(self):
        assert is_neutral(0.505)
        assert not is_neutral(0.65)


class TestBrier:
    def test_perfect_forecaster_scores_zero(self):
        assert brier_score([(1.0, 1), (0.0, 0)]) == pytest.approx(0.0, abs=1e-4)

    def test_constant_coin_flip_scores_a_quarter(self):
        """0.25 is the reference every reader knows — but it is table stakes,
        not the null. The honest null is the base rate on the same rows."""
        assert brier_score([(0.5, 1), (0.5, 0), (0.5, 1)]) == pytest.approx(0.25)

    def test_empty_is_none_not_zero(self):
        """A silent 0.0 would read as a perfect score."""
        assert brier_score([]) is None

    def test_murphy_identity_holds(self):
        """Brier = reliability - resolution + uncertainty. If this drifts, the
        decomposition is lying and `resolution` — the number that matters —
        cannot be trusted."""
        random.seed(11)
        pairs = [(random.random(), random.randint(0, 1)) for _ in range(600)]
        d = brier_decomposition(pairs)
        identity = d["reliability"] - d["resolution"] + d["uncertainty"]
        # Binning makes this approximate, not exact.
        assert identity == pytest.approx(d["brier"], abs=0.01)

    def test_resolution_is_zero_for_an_uninformative_forecaster(self):
        """Always predicting the base rate: perfectly reliable, zero resolution.
        This is the failure mode the repo has already measured — the system can
        spot its own bad decisions but cannot pick winners — so a panel that
        only improves reliability has bought nothing."""
        pairs = [(0.5, 1)] * 50 + [(0.5, 0)] * 50
        d = brier_decomposition(pairs)
        assert d["resolution"] == pytest.approx(0.0, abs=1e-6)
        assert d["reliability"] == pytest.approx(0.0, abs=1e-6)

    def test_resolution_is_positive_for_a_discriminating_forecaster(self):
        pairs = [(0.9, 1)] * 50 + [(0.1, 0)] * 50
        d = brier_decomposition(pairs)
        assert d["resolution"] > 0.2
        assert d["brier"] < 0.02

    def test_empty_decomposition_reports_none_not_zero(self):
        d = brier_decomposition([])
        assert d["n"] == 0 and d["brier"] is None
