"""
Unit tests for DatasetManifestBuilder.
Tests chronological partitioning, zero lookahead bias, and SHA-256 hashing.
Follows TDD green/red discipline.
Zero credential leakage: dynamically generated paths and IDs.
"""

import datetime
import json
import secrets
import tempfile
from pathlib import Path
import pytest

from app.services.dataset_manifest_builder import DatasetManifestBuilder, ManifestSplit


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def builder(temp_data_dir):
    return DatasetManifestBuilder(output_dir=temp_data_dir)


def test_chronological_partitioning(builder):
    base_time = datetime.datetime(2026, 9, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    samples = []
    for i in range(100):
        t = base_time + datetime.timedelta(hours=i)
        samples.append({
            "id": f"sample_{i}",
            "timestamp": t.isoformat(),
            "data": {"tokenized_text": [f"word_{i}"], "ner": []},
        })

    # Shuffle input to prove sorting occurs strictly by timestamp
    import random
    shuffled = list(samples)
    random.seed(42)
    random.shuffle(shuffled)

    split = builder.create_manifest_splits(shuffled, train_ratio=0.8, val_ratio=0.1, test_ratio=0.1)

    assert len(split.train_samples) == 80
    assert len(split.val_samples) == 10
    assert len(split.test_samples) == 10

    # Verify strict chronological separation: max(train) <= min(val) and max(val) <= min(test)
    train_max_t = max(s["timestamp"] for s in split.train_samples)
    val_min_t = min(s["timestamp"] for s in split.val_samples)
    val_max_t = max(s["timestamp"] for s in split.val_samples)
    test_min_t = min(s["timestamp"] for s in split.test_samples)

    assert train_max_t <= val_min_t, f"Train max {train_max_t} must be <= Val min {val_min_t}"
    assert val_max_t <= test_min_t, f"Val max {val_max_t} must be <= Test min {test_min_t}"


def test_build_and_write_manifest_files(builder, temp_data_dir):
    base_time = datetime.datetime(2026, 9, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    samples = []
    for i in range(20):
        t = base_time + datetime.timedelta(hours=i)
        samples.append({
            "id": f"sample_{i}",
            "timestamp": t.isoformat(),
            "tokenized_text": [f"Apple_{i}", "grew"],
            "ner": [[0, 0, "company"]],
        })

    manifest = builder.build_manifest(
        task="gliner_finetune",
        samples=samples,
        train_ratio=0.8,
        val_ratio=0.1,
        test_ratio=0.1,
    )

    assert manifest["task"] == "gliner_finetune"
    assert manifest["total_samples"] == 20
    assert manifest["splits"]["train_count"] == 16
    assert manifest["splits"]["val_count"] == 2
    assert manifest["splits"]["test_count"] == 2
    assert "sha256" in manifest
    assert len(manifest["sha256"]) == 64

    # Verify manifest file was written to disk
    manifest_file = Path(manifest["manifest_path"])
    assert manifest_file.exists()

    train_file = Path(manifest["train_path"])
    assert train_file.exists()
    with open(train_file, "r") as f:
        lines = [json.loads(line) for line in f]
    assert len(lines) == 16
