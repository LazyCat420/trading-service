"""`signal_weights` must be a canonical, finite, sum-to-one vector — and say where it came from.

WHY THIS EXISTS (measured 2026-09-06, Appendix M of the trading-cycle audit).
Across 1,134 `trade_results` rows carrying a weights dict:

  * 1,127 had the canonical key set; **7 did not**;
  * **2** summed to something other than 1.00 +/- 0.01;
  * **471 (41.5%)** were all-equal at exactly 0.25 — the equalised salvage
    default — with nothing in the row saying so.

The seventh malformed row is the specimen this file is built on. It is from
cycle-v3-1788646388 at 00:05:32 UTC and it went to an **executed order**
(ZS BUY 3.0808 @ $169.84):

    {'board': 0.45, 'quant': 0.25, 'specific': 0.0,
     'fundamental': 0.15, 'debate': 0.0, 'board_dup': 0.0}      sum = 0.85

Two invented keys, `debate` zeroed where its sibling SNOW had 0.15 on the same
stage of the same cycle, and a total of 0.85. Nothing validated it: the runner's
salvage branch fires only when `signal_weights` is *absent* (agent_runner.py),
`TRADE_DECISION_SCHEMA` types it as a bare `{"type": "object"}` with no shape,
and `save_trade_result` persists `verdict.get("signal_weights", {})` verbatim.

The tests assert PROPERTIES over swept inputs, not transcribed outputs — a
table of expected vectors would drift the moment the canonical set changes.
"""
from __future__ import annotations

import math
import random

import pytest

from app.v3.artifacts import (
    CANONICAL_SIGNAL_KEYS,
    normalize_signal_weights,
)

# The live row. Kept verbatim, including the invented keys, so this file
# remains the fixture for the defect even after the fix lands.
ZS_20260906_EXECUTED_BUY = {
    "board": 0.45,
    "quant": 0.25,
    "specific": 0.0,
    "fundamental": 0.15,
    "debate": 0.0,
    "board_dup": 0.0,
}

# Its healthy sibling from the same cycle, same stage (SNOW).
SNOW_20260906_EXECUTED_BUY = {
    "board": 0.45,
    "quant": 0.25,
    "fundamental": 0.15,
    "debate": 0.15,
}


def _assert_canonical(weights: dict) -> None:
    """Every invariant a persisted vector must satisfy, in one place."""
    assert set(weights) == set(CANONICAL_SIGNAL_KEYS), (
        f"key set drifted: {sorted(weights)}"
    )
    for k, v in weights.items():
        assert isinstance(v, float), f"{k} is {type(v).__name__}, not float"
        assert math.isfinite(v), f"{k} is not finite: {v!r}"
        assert v >= 0.0, f"{k} is negative: {v!r}"
    assert abs(sum(weights.values()) - 1.0) < 1e-9, (
        f"does not sum to 1: {sum(weights.values())!r}"
    )


class TestTheLiveSpecimen:
    def test_the_executed_zs_row_is_repaired_and_labelled(self):
        weights, source = normalize_signal_weights(ZS_20260906_EXECUTED_BUY)

        _assert_canonical(weights)
        assert source == "model_normalized"
        # The invented keys are gone, not folded into a canonical one.
        assert "board_dup" not in weights
        assert "specific" not in weights

    def test_repair_preserves_the_models_relative_ordering(self):
        """Renormalising must not re-rank the signals.

        This is the property that makes the repair safe: the model said board
        beat quant beat fundamental, and it still does afterwards. A repair
        that reordered them would be inventing a different decision.
        """
        weights, _ = normalize_signal_weights(ZS_20260906_EXECUTED_BUY)

        raw = ZS_20260906_EXECUTED_BUY
        for a in CANONICAL_SIGNAL_KEYS:
            for b in CANONICAL_SIGNAL_KEYS:
                if raw.get(a, 0.0) > raw.get(b, 0.0):
                    assert weights[a] > weights[b], f"{a} vs {b} re-ranked"

    def test_a_healthy_sibling_is_left_exactly_alone(self):
        weights, source = normalize_signal_weights(SNOW_20260906_EXECUTED_BUY)

        assert source == "model"
        assert weights == pytest.approx(SNOW_20260906_EXECUTED_BUY)


class TestProvenance:
    def test_absent_weights_are_the_equalised_default_and_say_so(self):
        for empty in (None, {}, [], "", 0):
            weights, source = normalize_signal_weights(empty)
            _assert_canonical(weights)
            assert source == "default_equalized", f"for {empty!r}"
            assert set(weights.values()) == {0.25}

    def test_a_vector_of_zeros_cannot_be_rescaled_so_it_falls_back(self):
        weights, source = normalize_signal_weights({k: 0.0 for k in CANONICAL_SIGNAL_KEYS})
        _assert_canonical(weights)
        assert source == "default_equalized"

    def test_only_junk_keys_reads_as_absent(self):
        weights, source = normalize_signal_weights({"specific": 0.6, "board_dup": 0.4})
        _assert_canonical(weights)
        assert source == "default_equalized"

    def test_an_equalised_vector_from_the_model_is_still_labelled_model(self):
        """0.25 x 4 is what the salvage default looks like, but a model may
        legitimately emit it. Provenance must come from the CALLER's knowledge
        of whether a vector was supplied — not from pattern-matching the
        values, which is how 471 historical rows became indistinguishable."""
        weights, source = normalize_signal_weights({k: 0.25 for k in CANONICAL_SIGNAL_KEYS})
        assert source == "model"
        assert weights == pytest.approx({k: 0.25 for k in CANONICAL_SIGNAL_KEYS})


class TestSweptInputs:
    def test_any_positive_vector_normalises(self):
        rng = random.Random(20260906)
        for _ in range(400):
            raw = {k: rng.uniform(0.0, 3.0) for k in CANONICAL_SIGNAL_KEYS}
            weights, source = normalize_signal_weights(raw)
            _assert_canonical(weights)
            total = sum(raw.values())
            assert source == ("model" if abs(total - 1.0) <= 0.01 else "model_normalized")

    def test_a_missing_canonical_key_is_filled_with_zero_not_dropped(self):
        for missing in CANONICAL_SIGNAL_KEYS:
            raw = {k: 0.25 for k in CANONICAL_SIGNAL_KEYS if k != missing}
            weights, source = normalize_signal_weights(raw)
            _assert_canonical(weights)
            assert weights[missing] == 0.0
            assert source == "model_normalized"

    @pytest.mark.parametrize(
        "bad",
        [float("nan"), float("inf"), float("-inf"), -0.5, "0.4", None, True],
    )
    def test_unusable_values_are_discarded_not_propagated(self, bad):
        raw = {"board": 0.5, "quant": 0.5, "fundamental": bad, "debate": 0.0}
        weights, _ = normalize_signal_weights(raw)
        _assert_canonical(weights)
        assert weights["fundamental"] == 0.0

    def test_key_casing_and_padding_do_not_create_a_new_signal(self):
        weights, source = normalize_signal_weights(
            {" Board ": 0.4, "QUANT": 0.3, "Fundamental": 0.2, "debate": 0.1}
        )
        _assert_canonical(weights)
        assert source == "model"
        assert weights["board"] == pytest.approx(0.4)

    def test_the_function_never_mutates_its_input(self):
        raw = dict(ZS_20260906_EXECUTED_BUY)
        before = dict(raw)
        normalize_signal_weights(raw)
        assert raw == before


class TestTheRunnerStampsProvenance:
    """The pure function is only half the fix — the runner has to call it."""

    def _decision(self, weights):
        d = {
            "action": "BUY",
            "confidence": 72,
            "reasoning": "Board verdict governs.",
        }
        if weights is not None:
            d["signal_weights"] = weights
        return d

    def test_a_malformed_vector_is_repaired_in_place_with_a_source(self):
        from app.v3.agent_runner import apply_signal_weights_policy

        artifact = self._decision(ZS_20260906_EXECUTED_BUY)
        apply_signal_weights_policy(
            artifact, artifact_type="trade_decision",
            agent_name="v3_decision_synthesizer", ticker="ZS",
        )

        _assert_canonical(artifact["signal_weights"])
        assert artifact["signal_weights_source"] == "model_normalized"

    def test_an_absent_vector_still_gets_the_equalised_default(self):
        from app.v3.agent_runner import apply_signal_weights_policy

        artifact = self._decision(None)
        apply_signal_weights_policy(
            artifact, artifact_type="trade_decision",
            agent_name="v3_decision_synthesizer", ticker="ZS",
        )

        assert artifact["signal_weights_source"] == "default_equalized"
        assert set(artifact["signal_weights"].values()) == {0.25}

    def test_a_decision_missing_its_required_fields_is_not_salvaged(self):
        """The pre-existing contract: without action/confidence/reasoning the
        runner must NOT invent weights, because the artifact is a failed run
        and the caller turns it into AGENT_ERROR."""
        from app.v3.agent_runner import apply_signal_weights_policy

        artifact = {"action": "", "confidence": None}
        salvaged = apply_signal_weights_policy(
            artifact, artifact_type="trade_decision",
            agent_name="v3_decision_synthesizer", ticker="ZS",
        )

        assert salvaged is False
        assert "signal_weights" not in artifact

    def test_a_non_decision_artifact_is_untouched(self):
        from app.v3.agent_runner import apply_signal_weights_policy

        artifact = {"summary": "a desk note"}
        apply_signal_weights_policy(
            artifact, artifact_type="desk_note",
            agent_name="v3_junior_analyst", ticker="ZS",
        )
        assert "signal_weights" not in artifact


class TestTheSaverPersistsProvenance:
    def test_save_trade_result_writes_signal_weights_source(self, monkeypatch):
        from app.services import trade_result_saver

        captured: list[dict] = []
        monkeypatch.setattr(
            trade_result_saver.mongo_store, "delete_docs", lambda *a, **k: None
        )
        monkeypatch.setattr(
            trade_result_saver.mongo_store,
            "insert_docs",
            lambda coll, docs: captured.extend(docs),
        )

        trade_result_saver.save_trade_result(
            cycle_id="cycle-test",
            ticker="ZS",
            verdict={
                "action": "BUY",
                "confidence": 72,
                "reasoning": "x",
                "signal_weights": ZS_20260906_EXECUTED_BUY,
                "signal_weights_source": "model_normalized",
            },
        )

        assert captured, "nothing was inserted"
        row = captured[0]
        assert row["signal_weights_source"] == "model_normalized"
        # Belt and braces: the saver is the last gate before the database, so
        # it normalises too. A verdict assembled by a path that skipped the
        # runner must not be able to persist an off-sum vector.
        _assert_canonical(row["signal_weights"])
