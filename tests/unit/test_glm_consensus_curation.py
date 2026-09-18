"""
Unit tests for GLM Consensus Curation, Failure Isolation, and Multi-Occurrence Spans.
Verifies Items 5 & 6:
- Separating annotation failure (malformed JSON, HTTP error) from valid negative labels (clean 0-entity consensus).
- Quarantining uncertain examples.
- Extracting exact start and end offsets for repeated entity occurrences in text.
- Validating label ontology enum.
- Specialist-specific curation formats for GLiNER, Market CNN, and Timeseries RNN.
"""

from unittest.mock import AsyncMock, patch
import pytest

from app.services.glm_curator_service import (
    AnnotationStatus,
    ConsensusEntity,
    CuratorAnnotationResult,
    GLMCuratorService,
)


@pytest.fixture
def curator():
    return GLMCuratorService(base_url="http://10.0.0.16:5591/vllm-shim/gold-spark")


def test_ontology_enforcement(curator):
    # Valid labels should pass
    valid_ent = ConsensusEntity(text="AAPL", label="ticker")
    assert curator.is_valid_label(valid_ent.label)

    # Hallucinated or unknown labels should fail
    invalid_ent = ConsensusEntity(text="Something", label="random_unsupported_label")
    assert not curator.is_valid_label(invalid_ent.label)


def test_extracts_all_occurrences_of_repeated_entities(curator):
    text = "Apple released earnings. Apple reported strong margins, and Apple announced a buyback."
    entities = [ConsensusEntity(text="Apple", label="company")]

    resolved = curator.resolve_all_spans_in_text(text, entities)
    assert len(resolved) == 3
    for r in resolved:
        assert text[r.start:r.end] == "Apple"


@pytest.mark.asyncio
async def test_separates_annotation_failure_from_valid_negatives(curator):
    # Case A: GLM returns malformed JSON on all passes
    with patch.object(curator, "_call_glm_chat", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "This is not json at all, error error"
        result: CuratorAnnotationResult = await curator.annotate_text_hardened(
            "Some financial text with no clear entities."
        )

        assert result.status == AnnotationStatus.FAILED
        assert result.error_message is not None
        assert "json" in result.error_message.lower() or "parse" in result.error_message.lower()

    # Case B: Valid negative sample (GLM cleanly returns valid JSON with 0 entities)
    with patch.object(curator, "_call_glm_chat", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"entities": []}'
        result_neg: CuratorAnnotationResult = await curator.annotate_text_hardened(
            "General economic commentary without company specifics."
        )

        assert result_neg.status == AnnotationStatus.VALID_NEGATIVE
        assert len(result_neg.entities) == 0


@pytest.mark.asyncio
async def test_quarantines_uncertain_examples(curator):
    # 3 passes: pass 1 extracts an entity, passes 2 and 3 return empty (1/3 agreement < 2/3 floor)
    responses = [
        '{"entities": [{"text": "NVIDIA", "label": "company"}]}',
        '{"entities": []}',
        '{"entities": []}',
    ]
    with patch.object(curator, "_call_glm_chat", side_effect=responses):
        result = await curator.annotate_text_hardened("Vague mention of tech sector.")
        assert result.status == AnnotationStatus.UNCERTAIN_QUARANTINE


def test_cnn_regime_curation_path(curator):
    # Generates 30x8 normalized tensor format and categorical regime label
    ohlcv_bars = [[100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i, 10000.0] for i in range(30)]
    regime_sample = curator.format_for_cnn_training(
        ticker="NVDA",
        ohlcv=ohlcv_bars,
        regime_label="BULL_TREND",
        timestamp="2026-09-15T16:00:00Z",
    )

    assert regime_sample["ticker"] == "NVDA"
    assert regime_sample["regime"] == "BULL_TREND"
    assert len(regime_sample["features"]) == 30
    assert len(regime_sample["features"][0]) == 8  # 5 OHLCV + 3 technical indicators


def test_rnn_timeseries_curation_path(curator):
    # Generates 25-step sequence paired with mature 5-day forward return quantiles
    seq_window = [[200.0 + i * 0.5 for _ in range(8)] for i in range(25)]
    forward_targets = {"p10": -0.02, "p50": 0.015, "p90": 0.05, "realized_5d": 0.018}

    rnn_sample = curator.format_for_rnn_training(
        ticker="AAPL",
        sequence=seq_window,
        targets=forward_targets,
        horizon_days=5,
        timestamp="2026-09-10T16:00:00Z",
    )

    assert rnn_sample["ticker"] == "AAPL"
    assert rnn_sample["horizon_days"] == 5
    assert len(rnn_sample["sequence"]) == 25
    assert rnn_sample["targets"]["p10"] <= rnn_sample["targets"]["p50"] <= rnn_sample["targets"]["p90"]
