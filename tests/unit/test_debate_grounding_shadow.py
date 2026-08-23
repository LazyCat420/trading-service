"""The debate grounding shadow: measures, never blocks (ch.90 fix A2).

The audit found ~1 in 7 checkable numbers in stored debate prose matched
nothing on the desk, and no validator covered any debate artifact. The shadow
diffs metric-tagged numbers against the verified baselines and attaches
counts. Contract: text byte-identical, no exception ever escapes, desk=None
means no shadow.
"""
import json
import logging
from unittest.mock import patch

from app.v3.artifact_validators import validate_artifact
from app.v3.shared_desk import SharedDesk

TRUTH_TECH = {"rsi": 41.2, "atr": 2.1, "sma_50": 100.0, "sma_200": 95.0}
TRUTH_FUND = {"pe_ratio": 24.0, "gross_margin": 0.302}
TRUTH_VAL = {"fcf_yield_pct": 4.1}


def _desk():
    return SharedDesk(ticker="TEST", cycle_id="cycle-g")


def _patched(fn):
    return patch.multiple(
        "app.quant.technical_baseline",
        compute_technical_baseline=lambda t: dict(TRUTH_TECH),
    ), patch.multiple(
        "app.quant.fundamental_block",
        compute_fundamental_baseline=lambda t: dict(TRUTH_FUND),
    ), patch.multiple(
        "app.quant.valuation_block",
        compute_valuation_baseline=lambda t: dict(TRUTH_VAL),
    )


def _run(artifact, artifact_type="bear_rebuttal", desk="default"):
    a, b, c = _patched(None)
    with a, b, c:
        return validate_artifact(
            artifact_type, artifact,
            desk=_desk() if desk == "default" else desk,
        )


def test_mismatch_is_counted_and_text_untouched():
    artifact = {
        "summary": "RSI-14 sits at 72 which is overbought, and gross margin "
                   "of 84.6% is best-in-class.",
        "rebuttals": [], "independent_risks": [], "confidence": 60,
    }
    before = json.dumps({k: v for k, v in artifact.items()})
    got = _run(dict(artifact))
    shadow = got["_grounding_shadow"]
    assert shadow["checked"] == 2
    assert shadow["mismatched"] == 2  # truth: rsi 41.2, gross_margin 30.2%
    assert {w["metric"] for w in shadow["worst"]} == {"rsi", "gross_margin"}
    assert json.dumps({k: v for k, v in got.items()
                       if k != "_grounding_shadow"}) == before, \
        "the shadow must never mutate the artifact's own fields"


def test_grounded_and_scale_equivalent_claims_pass():
    artifact = {
        "summary": "RSI-14 at 41.5 is neutral; gross margin 30.2% held; "
                   "P/E 24.3 is fair; price sits under the SMA-50 at $100.90.",
        "rebuttals": [], "independent_risks": [], "confidence": 60,
    }
    shadow = _run(dict(artifact))["_grounding_shadow"]
    assert shadow["checked"] == 4
    assert shadow["mismatched"] == 0, shadow


def test_untagged_numbers_are_not_claims():
    artifact = {"summary": "Revenue grew and the stock moved 7 points on 3x "
                           "volume near 450.",
                "rebuttals": [], "independent_risks": [], "confidence": 60}
    shadow = _run(dict(artifact))["_grounding_shadow"]
    assert shadow["checked"] == 0 and shadow["mismatched"] == 0


def test_structured_fields_are_scanned():
    artifact = {
        "summary": "prose without numbers",
        "verified_bull_claims": ["RSI-14 of 55 confirms momentum"],
        "unverified_bull_claims": [], "verified_bear_claims": [],
        "unverified_bear_claims": [], "winner": "bull", "final_confidence": 60,
    }
    shadow = _run(dict(artifact), artifact_type="debate_judge")["_grounding_shadow"]
    assert shadow["checked"] == 1
    assert shadow["mismatched"] == 1  # truth rsi 41.2


def test_truth_failure_is_logged_not_raised(caplog):
    artifact = {"summary": "RSI-14 at 72.", "rebuttals": [],
                "independent_risks": [], "confidence": 60}
    with patch("app.quant.technical_baseline.compute_technical_baseline",
               side_effect=RuntimeError("db down")):
        with caplog.at_level(logging.WARNING):
            got = validate_artifact("bear_rebuttal", dict(artifact),
                                    desk=_desk())
    # truth build failed entirely -> everything unverifiable, nothing raised
    assert got["_grounding_shadow"]["checked"] == 0
    assert got["_grounding_shadow"]["unverifiable"] >= 1
    assert any("GroundingShadow" in r.message for r in caplog.records)


def test_no_desk_means_no_shadow():
    got = validate_artifact("bear_rebuttal",
                            {"summary": "RSI-14 at 72.", "confidence": 60})
    assert "_grounding_shadow" not in got


def test_non_debate_types_are_untouched():
    got = _run({"summary": "RSI-14 at 72.", "key_findings": [],
                "data_gaps": [], "confidence": 60},
               artifact_type="desk_note")
    assert "_grounding_shadow" not in got
