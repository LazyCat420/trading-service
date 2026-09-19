"""
Unit tests for Dataset Manifest Integrity & Deduplication.
Verifies Item 4:
- Rejection of missing/invalid timestamps (zero tolerance for epoch 0 fallbacks).
- Content deduplication of related articles and overlapping windows.
- Independent per-split checksums (train_sha256, val_sha256, test_sha256).
- Train-only preprocessing normalization isolation.
- Temporal embargo gap between splits.
"""

import datetime
import json
import tempfile
from pathlib import Path
import pytest

from app.services.dataset_manifest_builder import DatasetManifestBuilder


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def builder(temp_dir):
    return DatasetManifestBuilder(output_dir=temp_dir)


def test_rejects_missing_or_invalid_timestamps(builder):
    samples = [
        {"id": "s1", "timestamp": "2026-09-10T12:00:00Z", "text": "Sample 1"},
        {"id": "s2", "timestamp": None, "text": "Sample 2"},  # Missing
        {"id": "s3", "timestamp": "not-a-timestamp", "text": "Sample 3"},  # Malformed
    ]

    with pytest.raises(ValueError) as exc_info:
        builder.create_manifest_splits(samples)
    assert "timestamp" in str(exc_info.value).lower()


def test_deduplicates_identical_or_near_duplicate_samples(builder):
    base_time = datetime.datetime(2026, 9, 10, 10, 0, 0, tzinfo=datetime.timezone.utc)
    samples = [
        {"id": "s1", "timestamp": (base_time + datetime.timedelta(minutes=1)).isoformat(), "text": "NVIDIA revenue surges 150%"},
        {"id": "s2", "timestamp": (base_time + datetime.timedelta(minutes=2)).isoformat(), "text": "Apple launches iPhone 18"},
        {"id": "s3", "timestamp": (base_time + datetime.timedelta(minutes=3)).isoformat(), "text": "NVIDIA revenue surges 150%"},  # Duplicate!
        {"id": "s4", "timestamp": (base_time + datetime.timedelta(minutes=4)).isoformat(), "text": "Microsoft buyback announced"},
    ]

    splits = builder.create_manifest_splits(samples, deduplicate=True)
    total_retained = len(splits.train_samples) + len(splits.val_samples) + len(splits.test_samples)
    assert total_retained == 3


def test_computes_independent_per_split_checksums(builder):
    base_time = datetime.datetime(2026, 9, 10, 10, 0, 0, tzinfo=datetime.timezone.utc)
    samples = [
        {
            "id": f"s_{i}",
            "timestamp": (base_time + datetime.timedelta(hours=i)).isoformat(),
            "text": f"Market report headline {i}",
            "tokenized_text": [f"word_{i}"],
            "ner": [],
        }
        for i in range(30)
    ]

    manifest = builder.build_manifest(
        task="gliner_finetune",
        samples=samples,
        train_ratio=0.8,
        val_ratio=0.1,
        test_ratio=0.1,
    )

    assert "train_sha256" in manifest["splits"]
    assert "val_sha256" in manifest["splits"]
    assert "test_sha256" in manifest["splits"]
    assert len(manifest["splits"]["train_sha256"]) == 64
    assert len(manifest["splits"]["val_sha256"]) == 64
    assert len(manifest["splits"]["test_sha256"]) == 64
    assert manifest["splits"]["train_sha256"] != manifest["splits"]["val_sha256"]


def test_enforces_temporal_embargo_gap_between_splits(builder):
    base_time = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    samples = [
        {
            "id": f"s_{i}",
            "timestamp": (base_time + datetime.timedelta(hours=i * 6)).isoformat(),
            "text": f"Headline at hour {i*6}",
        }
        for i in range(40)
    ]

    splits = builder.create_manifest_splits(
        samples,
        train_ratio=0.8,
        val_ratio=0.1,
        test_ratio=0.1,
        embargo_hours=12.0,
    )

    train_max_t = datetime.datetime.fromisoformat(splits.train_samples[-1]["timestamp"].replace("Z", "+00:00"))
    val_min_t = datetime.datetime.fromisoformat(splits.val_samples[0]["timestamp"].replace("Z", "+00:00"))
    val_max_t = datetime.datetime.fromisoformat(splits.val_samples[-1]["timestamp"].replace("Z", "+00:00"))
    test_min_t = datetime.datetime.fromisoformat(splits.test_samples[0]["timestamp"].replace("Z", "+00:00"))

    assert (val_min_t - train_max_t).total_seconds() >= 12 * 3600
    assert (test_min_t - val_max_t).total_seconds() >= 12 * 3600


def test_enforces_purge_and_embargo_isolation(builder):
    """Verifies that sequence lookback + horizon (purge) and embargo gap isolate splits."""
    base_time = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    samples = [
        {
            "id": f"s_{i}",
            "timestamp": (base_time + datetime.timedelta(hours=i * 4)).isoformat(),
            "text": f"Bar sequence at hour {i*4}",
        }
        for i in range(200)
    ]

    splits = builder.create_manifest_splits(
        samples,
        train_ratio=0.8,
        val_ratio=0.1,
        test_ratio=0.1,
        embargo_hours=12.0,
        purge_hours=24.0,  # 36 hours total isolation
    )

    train_max_t = datetime.datetime.fromisoformat(splits.train_samples[-1]["timestamp"].replace("Z", "+00:00"))
    val_min_t = datetime.datetime.fromisoformat(splits.val_samples[0]["timestamp"].replace("Z", "+00:00"))
    val_max_t = datetime.datetime.fromisoformat(splits.val_samples[-1]["timestamp"].replace("Z", "+00:00"))
    test_min_t = datetime.datetime.fromisoformat(splits.test_samples[0]["timestamp"].replace("Z", "+00:00"))

    assert (val_min_t - train_max_t).total_seconds() >= 36 * 3600
    assert (test_min_t - val_max_t).total_seconds() >= 36 * 3600


def test_normalization_fitted_strictly_on_train(builder):
    """Verifies that normalizer is fit exclusively on train partition, with zero leakage from val or test."""
    base_time = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    # Train has values 10..20, Val has values 100..110, Test has values 1000..1010
    samples = []
    for i in range(20):
        t = base_time + datetime.timedelta(hours=i)
        if i < 16:
            val = float(10 + i)  # Train: 10 to 25
        elif i < 18:
            val = float(500 + i)  # Val: 516, 517
        else:
            val = float(5000 + i)  # Test: 5018, 5019
        samples.append({
            "id": f"s_{i}",
            "timestamp": t.isoformat(),
            "close": val,
            "volume": val * 2,
        })

    splits = builder.create_manifest_splits(samples, train_ratio=0.8, val_ratio=0.1, test_ratio=0.1)
    norm_splits, stats = DatasetManifestBuilder.fit_and_apply_normalization(splits, ["close", "volume"])

    # Verify mean is strictly calculated on train
    train_closes = [s["close"] for s in splits.train_samples]
    expected_mean = sum(train_closes) / len(train_closes)
    assert abs(stats["close"]["mean"] - expected_mean) < 1e-6
    # If val/test leaked into normalization, stats["close"]["mean"] would be >> 100
    assert stats["close"]["mean"] < 30.0

    # Verify val and test samples were normalized using train parameters
    for s in norm_splits.val_samples:
        expected_norm = (s["close"] - stats["close"]["mean"]) / stats["close"]["std"]
        assert abs(s["close_normalized"] - expected_norm) < 1e-6


def test_holdout_protection_rejects_copied_dataset_by_content_hash(builder):
    """Verifies that holdouts are protected by content hash, rejecting renamed/copied datasets."""
    base_time = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    holdout_samples = [
        {"id": f"holdout_{i}", "timestamp": (base_time + datetime.timedelta(hours=i)).isoformat(), "text": f"Protected event {i}"}
        for i in range(10)
    ]
    # Build holdout manifest
    holdout_manifest = builder.build_manifest(
        task="gliner_finetune",
        samples=holdout_samples,
        metadata={"is_holdout": True, "role": "holdout"}
    )
    protected_hashes = set(holdout_manifest["holdout_content_hashes"])
    assert len(protected_hashes) > 0

    # Now attempt to train on a dataset that renamed the file/samples but contains protected content
    sneaky_samples = [
        {"id": "innocent_sample_1", "timestamp": (base_time + datetime.timedelta(days=1)).isoformat(), "text": "Ordinary news"},
        {"id": "renamed_holdout_leak", "timestamp": (base_time + datetime.timedelta(days=2)).isoformat(), "text": "Protected event 9"},  # Leaked!
    ]

    with pytest.raises(ValueError) as exc_info:
        builder.build_manifest(
            task="gliner_finetune",
            samples=sneaky_samples,
            metadata={"protected_holdout_hashes": list(protected_hashes)}
        )
    assert "collide with protected holdout data" in str(exc_info.value)

