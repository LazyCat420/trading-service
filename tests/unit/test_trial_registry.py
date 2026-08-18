"""The multiple-testing denominator must be a fact, not a guess.

`deflated_sharpe_ratio` is the only gate in this repo that fails a best-of-100
pure-noise winner (tests/unit/test_multiple_testing_gates.py). It is exactly
as honest as the `n_trials` handed to it, and until now nothing recorded that:
scripts/factor_backtest.py passed `len(factor_names)` = 4 with its own comment
calling that a floor.

These tests use an in-memory fake for the DB so they pin the LOGIC — that
re-running a hypothesis is not a new trial, that the trial being judged counts
itself, and that a bigger denominator actually deflates harder.
"""

import numpy as np
import pytest

from app.quant import trial_registry
from app.quant.stat_gates import deflated_sharpe_ratio


class _FakeMongoStore:
    """In-memory stand-in for the `research_trials` collection.

    Keyed on (family, label) — the uniqueness the registry relies on to make
    `record_trial` idempotent. Implements only the three calls the module
    makes: `update_docs` (upsert + $inc), `upsert_doc` (insert_only) and
    `count_docs`.
    """

    def __init__(self, store):
        self.store = store

    def update_docs(self, collection, filt, update, upsert=False, **kwargs):
        assert collection == "research_trials"
        key = (filt["family"], filt["label"])
        doc = self.store.get(key)
        if doc is None:
            if not upsert:
                return 0
            # $setOnInsert seeds the new document, then the normal operators
            # apply — the same order Mongo uses.
            doc = dict(update.get("$setOnInsert", {}))
            doc.setdefault("run_count", 0)
            self.store[key] = doc
        for field, delta in update.get("$inc", {}).items():
            doc[field] = doc.get(field, 0) + delta
        doc.update(update.get("$set", {}))
        return 1

    def upsert_doc(self, collection, filt, doc, insert_only=False, **kwargs):
        assert collection == "research_trials"
        key = (filt["family"], filt["label"])
        if key in self.store and insert_only:
            # Seeding must not reset an existing row's run_count.
            return 0
        self.store[key] = dict(doc)
        return 1

    def count_docs(self, collection, filt=None, **kwargs):
        assert collection == "research_trials"
        filt = filt or {}
        return sum(
            1 for (family, label) in self.store
            if ("family" not in filt or family == filt["family"])
            and ("label" not in filt or label == filt["label"])
        )


@pytest.fixture()
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(trial_registry, "mongo_store", _FakeMongoStore(data))
    return data


# ── what counts as a trial ───────────────────────────────────────────

def test_a_rerun_is_not_a_new_trial(store):
    """An automated harness that loops must not inflate its own denominator
    — that would make the correction weaker the more you search, which is
    exactly backwards."""
    for _ in range(5):
        trial_registry.record_trial("factor:momentum", source="test")
    assert trial_registry.trial_count() == 1
    assert store[("price_derived", "factor:momentum")]["run_count"] == 5


def test_a_variant_is_a_new_trial(store):
    trial_registry.record_trial("factor:momentum_12_1")
    trial_registry.record_trial("factor:momentum_6_1")
    assert trial_registry.trial_count() == 2


def test_families_are_scored_separately(store):
    trial_registry.record_trial("a", family="price_derived")
    trial_registry.record_trial("b", family="price_derived")
    trial_registry.record_trial("c", family="vol_forecast")
    assert trial_registry.trial_count("price_derived") == 2
    assert trial_registry.trial_count("vol_forecast") == 1


def test_an_unrecorded_trial_counts_itself(store):
    """The hypothesis you are about to judge is a draw too. Without this the
    very first result out of a fresh ledger would deflate against zero."""
    trial_registry.record_trial("factor:momentum")
    assert trial_registry.trial_count() == 1
    assert trial_registry.trial_count(include="regime:brand_new") == 2
    # ...and once recorded it is not double-counted.
    trial_registry.record_trial("regime:brand_new")
    assert trial_registry.trial_count(include="regime:brand_new") == 2


def test_count_never_returns_zero(store):
    assert trial_registry.trial_count() == 1


def test_blank_label_refused(store):
    assert trial_registry.record_trial("") is False
    assert trial_registry.record_trial("   ") is False
    assert not store


def test_seeding_is_idempotent(store):
    first = trial_registry.seed_known_trials()
    second = trial_registry.seed_known_trials()
    assert first == second == len(trial_registry.KNOWN_PRIOR_TRIALS)


def test_seed_covers_the_hypotheses_the_dsr_docstring_names(store):
    """deflated_sharpe_ratio's docstring lists what has been run against this
    price history: momentum, low-vol, beta, reversal, sizing rules, HMM
    regimes. If the seed drifts from that list the denominator silently
    understates again."""
    trial_registry.seed_known_trials()
    labels = {label for (_f, label) in store}
    for expected in ("factor:momentum_12_1", "factor:low_volatility_61d",
                     "factor:market_beta_253d", "factor:short_term_reversal_21d",
                     "regime:hmm_2_state", "sizing:atr_risk_bracket"):
        assert expected in labels, f"{expected} missing from the seed"


# ── the denominator actually bites ───────────────────────────────────

def test_more_trials_deflate_harder():
    """The whole point: the same return series must look less impressive when
    it is the winner of more searches."""
    rng = np.random.default_rng(11)
    rets = rng.normal(0.05, 1.0, 500)

    few = deflated_sharpe_ratio(rets, n_trials=4)
    many = deflated_sharpe_ratio(rets, n_trials=40)
    assert many["expected_max_sharpe_from_luck"] > few["expected_max_sharpe_from_luck"]
    assert many["dsr"] <= few["dsr"]


def test_registry_backed_dsr_labels_its_source(store):
    """A report must never present a ledger-backed deflation and a guessed
    one as the same number."""
    trial_registry.seed_known_trials()
    rng = np.random.default_rng(3)
    out = trial_registry.deflated_sharpe_from_registry(
        rng.normal(0.05, 1.0, 400), label="factor:momentum_12_1",
    )
    assert out["n_trials_source"] == "research_trials[price_derived]"
    assert out["trial_label"] == "factor:momentum_12_1"
    assert out["n_trials"] == len(trial_registry.KNOWN_PRIOR_TRIALS)


def test_registry_dsr_records_the_trial_it_judges(store):
    rng = np.random.default_rng(5)
    trial_registry.deflated_sharpe_from_registry(
        rng.normal(0.05, 1.0, 400), label="regime:overlay_v1",
    )
    assert ("price_derived", "regime:overlay_v1") in store


def test_registry_failure_degrades_to_one_not_to_a_crash(monkeypatch):
    """A ledger outage must not stop research; it just means no deflation."""
    class _BrokenStore:
        def __getattr__(self, _name):
            def _boom(*a, **k):
                raise RuntimeError("db down")
            return _boom

    monkeypatch.setattr(trial_registry, "mongo_store", _BrokenStore())
    assert trial_registry.trial_count() == 1
    assert trial_registry.record_trial("x") is False
