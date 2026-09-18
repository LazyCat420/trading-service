"""
DatasetManifestBuilder — Packages curated training data into immutable, time-partitioned manifests.

Enforces zero lookahead bias by partitioning strictly chronologically:
- 80% train partition
- 10% validation partition
- 10% holdout test partition (frozen)
Generates SHA-256 data integrity hashes and writes JSONL artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
import uuid

logger = logging.getLogger(__name__)


@dataclass
class ManifestSplit:
    train_samples: list[dict[str, Any]]
    val_samples: list[dict[str, Any]]
    test_samples: list[dict[str, Any]]


class DatasetManifestBuilder:
    """Builds and packages training dataset manifests with chronological integrity."""

    def __init__(self, output_dir: str | Path | None = None):
        default_dir = Path("/tmp/trading_datasets")
        self.output_dir = Path(output_dir) if output_dir else default_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _parse_timestamp(sample: dict[str, Any]) -> datetime.datetime:
        """Extracts timestamp from sample, defaulting to UTC epoch if missing."""
        ts = sample.get("timestamp") or sample.get("published_at") or sample.get("created_at")
        if not ts:
            return datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)
        if isinstance(ts, (int, float)):
            return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        try:
            # Handle ISO string with or without Z
            cleaned = str(ts).replace("Z", "+00:00")
            return datetime.datetime.fromisoformat(cleaned)
        except Exception:
            return datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)

    def create_manifest_splits(
        self,
        samples: list[dict[str, Any]],
        train_ratio: float = 0.8,
        val_ratio: float = 0.1,
        test_ratio: float = 0.1,
    ) -> ManifestSplit:
        """
        Sorts samples strictly chronologically and partitions them into
        train, validation, and holdout test splits without lookahead bias.
        """
        if not samples:
            return ManifestSplit([], [], [])

        # Strict chronological sort
        sorted_samples = sorted(samples, key=self._parse_timestamp)
        n = len(sorted_samples)

        train_end = int(n * train_ratio)
        val_end = int(n * (train_ratio + val_ratio))

        # Ensure at least 1 sample in validation and test if n is small and ratios > 0
        if n >= 3:
            train_end = min(train_end, n - 2)
            val_end = min(max(val_end, train_end + 1), n - 1)

        train_samples = sorted_samples[:train_end]
        val_samples = sorted_samples[train_end:val_end]
        test_samples = sorted_samples[val_end:]

        return ManifestSplit(
            train_samples=train_samples,
            val_samples=val_samples,
            test_samples=test_samples,
        )

    def build_manifest(
        self,
        task: str,
        samples: list[dict[str, Any]],
        train_ratio: float = 0.8,
        val_ratio: float = 0.1,
        test_ratio: float = 0.1,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Partitions samples, serializes JSONL files, computes SHA-256 integrity hash,
        and returns manifest descriptor dict.
        """
        manifest_id = f"manifest-{uuid.uuid4().hex[:12]}"
        task_dir = self.output_dir / task / manifest_id
        task_dir.mkdir(parents=True, exist_ok=True)

        splits = self.create_manifest_splits(
            samples,
            train_ratio=train_ratio,
            val_ratio=val_ratio,
            test_ratio=test_ratio,
        )

        train_path = task_dir / "train.jsonl"
        val_path = task_dir / "val.jsonl"
        test_path = task_dir / "test.jsonl"

        hasher = hashlib.sha256()

        def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
            with open(path, "w", encoding="utf-8") as f:
                for item in items:
                    line = json.dumps(item, sort_keys=True, ensure_ascii=True)
                    f.write(line + "\n")
                    hasher.update(line.encode("utf-8"))

        _write_jsonl(train_path, splits.train_samples)
        _write_jsonl(val_path, splits.val_samples)
        _write_jsonl(test_path, splits.test_samples)

        sha256_hash = hasher.hexdigest()

        manifest_descriptor = {
            "manifest_id": manifest_id,
            "task": task,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sha256": sha256_hash,
            "total_samples": len(samples),
            "splits": {
                "train_count": len(splits.train_samples),
                "val_count": len(splits.val_samples),
                "test_count": len(splits.test_samples),
            },
            "train_path": str(train_path),
            "val_path": str(val_path),
            "test_path": str(test_path),
            "metadata": metadata or {},
        }

        manifest_path = task_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_descriptor, f, indent=2)

        manifest_descriptor["manifest_path"] = str(manifest_path)
        return manifest_descriptor
