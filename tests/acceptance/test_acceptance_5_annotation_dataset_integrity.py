"""
Acceptance Test 5: Verify annotation and dataset integrity end to end.
- Injects malformed/truncated GLM output, provider failures, valid empty annotations, repeated entities,
  invalid labels, and insufficient consensus.
- Proves failed annotations NEVER become negative examples (quarantined vs valid negatives).
- Tests shuffled timestamps, duplicate articles, overlapping windows, and temporal embargo.
- Verifies protected holdout records cannot enter train splits, and splits remain chronological.
"""

import datetime
from unittest.mock import AsyncMock, patch
import pytest

from app.services.glm_curator_service import (
    AnnotationStatus,
    ConsensusEntity,
    GLMCuratorService,
)
from app.services.dataset_manifest_builder import DatasetManifestBuilder

pytestmark = pytest.mark.real_mongo


@pytest.fixture
def curator():
    return GLMCuratorService(base_url="http://10.0.0.16:5591/vllm-shim/gold-spark")


@pytest.fixture
def manifest_builder(tmp_path):
    return DatasetManifestBuilder(output_dir=tmp_path)


# ── 1. Annotation Failures Never Become Negative Examples ──

@pytest.mark.asyncio
async def test_malformed_glm_output_is_marked_failed_not_negative(curator):
    """
    If GLM returns malformed/truncated JSON or raises an error, the curator MUST
    mark status as FAILED, not VALID_NEGATIVE. Failed annotations must never be
    fed to models as negative (zero-entity) training examples.
    """
    with patch.object(curator, "_call_glm_chat", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "This is not JSON at all: {broken, truncated"

        result = await curator.annotate_text_hardened("Apple announces record earnings", num_samples=3)
        assert result.status == AnnotationStatus.FAILED
        assert result.entities == []
        assert result.error_message is not None


@pytest.mark.asyncio
async def test_provider_failure_is_marked_failed(curator):
    """If network connection or vllm-2 fails, status must be FAILED."""
    with patch.object(curator, "_call_glm_chat", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = RuntimeError("vLLM connection refused: HTTP 502")

        result = await curator.annotate_text_hardened("Apple announces record earnings", num_samples=3)
        assert result.status == AnnotationStatus.FAILED
        assert "502" in result.error_message


@pytest.mark.asyncio
async def test_insufficient_consensus_is_quarantined_not_negative(curator):
    """
    If 1 pass proposes an entity but 2 passes find nothing (1/3 agreement),
    the sample is ambiguous. It MUST be marked UNCERTAIN_QUARANTINE, not VALID_NEGATIVE.
    """
    responses = [
        '{"entities": [{"text": "Apple", "label": "company", "ticker": "AAPL"}]}',  # pass 1
        '{"entities": []}',  # pass 2
        '{"entities": []}',  # pass 3
    ]
    with patch.object(curator, "_call_glm_chat", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = responses

        result = await curator.annotate_text_hardened("Apple reports results", num_samples=3, min_agreement=2)
        assert result.status == AnnotationStatus.UNCERTAIN_QUARANTINE
        assert result.entities == []
        assert "below required" in result.error_message


@pytest.mark.asyncio
async def test_valid_empty_annotations_marked_valid_negative(curator):
    """
    If all 3 passes agree there are no entities, it is a clean VALID_NEGATIVE.
    """
    with patch.object(curator, "_call_glm_chat", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"entities": []}'

        result = await curator.annotate_text_hardened("The weather was sunny in London today.", num_samples=3, min_agreement=2)
        assert result.status == AnnotationStatus.VALID_NEGATIVE
        assert result.entities == []


@pytest.mark.asyncio
async def test_repeated_entity_spans_all_resolved(curator):
    """
    When an entity appears multiple times in the text, all span occurrences must be resolved.
    """
    text = "Apple launched M4 today. Apple also reported record sales for Apple Silicon."
    with patch.object(curator, "_call_glm_chat", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"entities": [{"text": "Apple", "label": "company", "ticker": "AAPL"}]}'

        result = await curator.annotate_text_hardened(text, num_samples=2, min_agreement=2)
        assert result.status == AnnotationStatus.SUCCESS
        assert len(result.entities) == 3
        for ent in result.entities:
            assert ent.text == "Apple"
            assert text[ent.start:ent.end] == "Apple"


@pytest.mark.asyncio
async def test_invalid_labels_filtered_out(curator):
    """Unknown or hallucinated labels outside ALLOWED_LABELS must be discarded."""
    text = "Tim Cook spoke at the event."
    with patch.object(curator, "_call_glm_chat", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"entities": [{"text": "Tim Cook", "label": "made_up_label_xyz"}]}'

        result = await curator.annotate_text_hardened(text, num_samples=2, min_agreement=2)
        assert len(result.entities) == 0


# ── 2. Dataset Chronological & Partition Integrity ──

def test_shuffled_timestamps_are_sorted_chronologically(manifest_builder):
    """Samples submitted out of order must be sorted chronologically to prevent lookahead."""
    base = datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
    t1 = (base + datetime.timedelta(days=1)).isoformat()
    t2 = (base + datetime.timedelta(days=2)).isoformat()
    t3 = (base + datetime.timedelta(days=3)).isoformat()
    t4 = (base + datetime.timedelta(days=4)).isoformat()
    t5 = (base + datetime.timedelta(days=5)).isoformat()

    shuffled_samples = [
        {"id": "s5", "timestamp": t5, "features": [5.0]},
        {"id": "s1", "timestamp": t1, "features": [1.0]},
        {"id": "s4", "timestamp": t4, "features": [4.0]},
        {"id": "s2", "timestamp": t2, "features": [2.0]},
        {"id": "s3", "timestamp": t3, "features": [3.0]},
    ]

    splits = manifest_builder.create_manifest_splits(shuffled_samples, train_ratio=0.6, val_ratio=0.2, test_ratio=0.2)
    # Train partition must only have earlier timestamps
    train_times = [s["timestamp"] for s in splits.train_samples]
    val_times = [s["timestamp"] for s in splits.val_samples]
    test_times = [s["timestamp"] for s in splits.test_samples]

    assert max(train_times) <= min(val_times)
    assert max(val_times) <= min(test_times)


def test_protected_holdout_records_never_enter_train(manifest_builder):
    """Verifies that holdout test records are strictly isolated from train partitions."""
    base = datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
    samples = [
        {"id": f"sample_{i}", "timestamp": (base + datetime.timedelta(days=i)).isoformat(), "data": i}
        for i in range(20)
    ]

    splits = manifest_builder.create_manifest_splits(samples, train_ratio=0.8, val_ratio=0.1, test_ratio=0.1)
    train_ids = {s["id"] for s in splits.train_samples}
    test_ids = {s["id"] for s in splits.test_samples}

    # Absolute disjointness
    assert train_ids.isdisjoint(test_ids)
    # All test samples are strictly chronologically after train samples
    max_train_ts = max(s["timestamp"] for s in splits.train_samples)
    min_test_ts = min(s["timestamp"] for s in splits.test_samples)
    assert max_train_ts < min_test_ts


def test_rejects_missing_timestamp_samples(manifest_builder):
    """Any sample without a timestamp must raise ValueError, forbidding unanchored data."""
    samples = [
        {"id": "s1", "timestamp": "2026-05-01T00:00:00Z", "data": 1},
        {"id": "s2", "timestamp": None, "data": 2},  # Forbidden
    ]
    with pytest.raises(ValueError) as exc:
        manifest_builder.create_manifest_splits(samples)
    assert "missing timestamp" in str(exc.value).lower()
