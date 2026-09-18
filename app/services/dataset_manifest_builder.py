"""
DatasetManifestBuilder — Packages curated training data into immutable, time-partitioned manifests.

Enforces zero lookahead bias by partitioning strictly chronologically:
- 80% train partition
- 10% validation partition
- 10% holdout test partition (frozen)
Enforces strict timestamp validity (rejects missing/invalid timestamps with ValueError).
Provides content deduplication, temporal embargo gaps, and independent per-split SHA-256 integrity checksums.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional
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
        """Extracts timestamp from sample, rejecting missing or invalid timestamps."""
        ts = sample.get("timestamp") or sample.get("published_at") or sample.get("created_at")
        sample_id = sample.get("id", "unknown")
        if not ts:
            raise ValueError(f"Sample '{sample_id}' has missing timestamp. Chronological splits require valid timestamps.")

        if isinstance(ts, (int, float)):
            try:
                return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            except Exception as e:
                raise ValueError(f"Sample '{sample_id}' has invalid numeric timestamp '{ts}': {e}") from e

        try:
            # Handle ISO string with or without Z
            cleaned = str(ts).replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except Exception as e:
            raise ValueError(f"Sample '{sample_id}' has unparseable ISO timestamp '{ts}': {e}") from e

    def create_manifest_splits(
        self,
        samples: list[dict[str, Any]],
        train_ratio: float = 0.8,
        val_ratio: float = 0.1,
        test_ratio: float = 0.1,
        deduplicate: bool = True,
        embargo_hours: float = 0.0,
    ) -> ManifestSplit:
        """
        Sorts samples strictly chronologically and partitions them into
        train, validation, and holdout test splits without lookahead bias.
        Optionally deduplicates identical content and applies temporal embargo gaps.
        """
        if not samples:
            return ManifestSplit([], [], [])

        # Validate and parse timestamps, associating each with its sample
        stamped_samples: list[tuple[datetime.datetime, dict[str, Any]]] = []
        seen_content_hashes: set[str] = set()

        for sample in samples:
            dt = self._parse_timestamp(sample)

            if deduplicate:
                # Content deduplication key: normalized text, tokens, or id
                raw_content = (
                    sample.get("text")
                    or sample.get("raw_text")
                    or (json.dumps(sample.get("tokenized_text", []), sort_keys=True) if sample.get("tokenized_text") else None)
                    or sample.get("id")
                    or ""
                )
                content_norm = " ".join(str(raw_content).strip().lower().split())
                content_hash = hashlib.sha256(content_norm.encode("utf-8")).hexdigest()
                if content_hash in seen_content_hashes:
                    continue
                seen_content_hashes.add(content_hash)

            stamped_samples.append((dt, sample))

        if not stamped_samples:
            return ManifestSplit([], [], [])

        # Strict chronological sort
        stamped_samples.sort(key=lambda item: item[0])
        sorted_samples = [item[1] for item in stamped_samples]
        n = len(sorted_samples)

        train_end = int(n * train_ratio)
        val_end = int(n * (train_ratio + val_ratio))

        # Ensure at least 1 sample in validation and test if n is small and ratios > 0
        if n >= 3:
            train_end = min(train_end, n - 2)
            val_end = min(max(val_end, train_end + 1), n - 1)

        raw_train = sorted_samples[:train_end]
        raw_val = sorted_samples[train_end:val_end]
        raw_test = sorted_samples[val_end:]

        # Apply temporal embargo gaps between splits if requested
        if embargo_hours > 0.0 and raw_train and raw_val and raw_test:
            embargo_delta = datetime.timedelta(hours=embargo_hours)
            train_max_dt = self._parse_timestamp(raw_train[-1])

            # Filter validation samples to be after train_max_dt + embargo
            filtered_val = [s for s in raw_val if self._parse_timestamp(s) >= train_max_dt + embargo_delta]
            if not filtered_val and raw_val:
                filtered_val = [raw_val[-1]]  # preserve boundary

            val_max_dt = self._parse_timestamp(filtered_val[-1])
            filtered_test = [s for s in raw_test if self._parse_timestamp(s) >= val_max_dt + embargo_delta]
            if not filtered_test and raw_test:
                filtered_test = [raw_test[-1]]

            return ManifestSplit(
                train_samples=raw_train,
                val_samples=filtered_val,
                test_samples=filtered_test,
            )

        return ManifestSplit(
            train_samples=raw_train,
            val_samples=raw_val,
            test_samples=raw_test,
        )

    def build_manifest(
        self,
        task: str,
        samples: list[dict[str, Any]],
        train_ratio: float = 0.8,
        val_ratio: float = 0.1,
        test_ratio: float = 0.1,
        deduplicate: bool = True,
        embargo_hours: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Partitions samples, serializes JSONL files, computes independent per-split
        SHA-256 hashes as well as overall manifest hash, and writes manifest descriptor.
        """
        manifest_id = f"manifest-{uuid.uuid4().hex[:12]}"
        task_dir = self.output_dir / task / manifest_id
        task_dir.mkdir(parents=True, exist_ok=True)

        splits = self.create_manifest_splits(
            samples,
            train_ratio=train_ratio,
            val_ratio=val_ratio,
            test_ratio=test_ratio,
            deduplicate=deduplicate,
            embargo_hours=embargo_hours,
        )

        train_path = task_dir / "train.jsonl"
        val_path = task_dir / "val.jsonl"
        test_path = task_dir / "test.jsonl"

        aggregate_hasher = hashlib.sha256()

        def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> str:
            split_hasher = hashlib.sha256()
            with open(path, "w", encoding="utf-8") as f:
                for item in items:
                    line = json.dumps(item, sort_keys=True, ensure_ascii=True)
                    f.write(line + "\n")
                    line_bytes = line.encode("utf-8")
                    split_hasher.update(line_bytes)
                    aggregate_hasher.update(line_bytes)
            return split_hasher.hexdigest()

        train_sha256 = _write_jsonl(train_path, splits.train_samples)
        val_sha256 = _write_jsonl(val_path, splits.val_samples)
        test_sha256 = _write_jsonl(test_path, splits.test_samples)

        sha256_hash = aggregate_hasher.hexdigest()

        manifest_descriptor = {
            "manifest_id": manifest_id,
            "task": task,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sha256": sha256_hash,
            "total_samples": len(splits.train_samples) + len(splits.val_samples) + len(splits.test_samples),
            "splits": {
                "train_count": len(splits.train_samples),
                "val_count": len(splits.val_samples),
                "test_count": len(splits.test_samples),
                "train_sha256": train_sha256,
                "val_sha256": val_sha256,
                "test_sha256": test_sha256,
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
