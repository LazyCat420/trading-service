"""Anytime-valid sequential test (e-process) — the math must be right, or every
experiment verdict built on it is wrong.
"""

import math

import pytest

from app.autoresearch.sequential import eprocess_bernoulli, paired_disagreement_test


class TestEProcess:
    def test_no_data_is_no_evidence(self):
        assert eprocess_bernoulli(0, 0) == 1.0

    def test_balanced_outcomes_favour_the_null(self):
        # 10-10 is exactly what H0 predicts — evidence must not exceed 1.
        assert eprocess_bernoulli(10, 10) < 1.0

    def test_lopsided_outcomes_accumulate_evidence(self):
        assert eprocess_bernoulli(15, 5) > eprocess_bernoulli(12, 8) > eprocess_bernoulli(10, 10)

    def test_symmetry(self):
        # Evidence against p=0.5 is direction-agnostic.
        assert eprocess_bernoulli(15, 5) == pytest.approx(eprocess_bernoulli(5, 15))

    def test_known_value_all_wins(self):
        # E for k=n: Beta(n+1/2, 1/2)/Beta(1/2,1/2) * 2^n — check n=5 by hand.
        n = 5
        expected = math.exp(
            (math.lgamma(5.5) + math.lgamma(0.5) - math.lgamma(6.0))
            - (2 * math.lgamma(0.5) - math.lgamma(1.0))
            + n * math.log(2)
        )
        assert eprocess_bernoulli(5, 0) == pytest.approx(expected)

    def test_significance_needs_real_evidence(self):
        # A 6-1 split must NOT clear the alpha=0.05 bar (e >= 20) — tiny
        # samples shouldn't be able to end an experiment.
        assert eprocess_bernoulli(6, 1) < 20
        # A 20-2 split should.
        assert eprocess_bernoulli(20, 2) > 20

    def test_overflow_safe(self):
        assert eprocess_bernoulli(5000, 100) > 1e100  # finite, no OverflowError

    def test_negative_counts_rejected(self):
        with pytest.raises(ValueError):
            eprocess_bernoulli(-1, 3)


class TestPairedDisagreementTest:
    def test_ties_are_uninformative(self):
        # Both right / both wrong says nothing about which system is better.
        result = paired_disagreement_test([(True, True), (False, False)] * 10)
        assert result["informative_pairs"] == 0
        assert result["ties"] == 20
        assert result["e_value"] == 1.0

    def test_challenger_domination_detected(self):
        pairs = [(False, True)] * 20 + [(True, False)] * 2
        result = paired_disagreement_test(pairs)
        assert result["leader"] == "challenger"
        assert result["e_value"] >= 20
        assert "significant" in result["verdict"] or "strong" in result["verdict"]

    def test_even_split_favours_null(self):
        result = paired_disagreement_test([(False, True)] * 10 + [(True, False)] * 10)
        assert result["e_value"] < 1
        assert result["verdict"] == "evidence favours no-difference"

    def test_small_sample_stays_inconclusive(self):
        result = paired_disagreement_test([(False, True)] * 3)
        assert result["e_value"] < 20
        assert result["leader"] == "challenger"
