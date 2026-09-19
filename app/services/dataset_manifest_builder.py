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

    @staticmethod
    def compute_sample_hash(sample: dict[str, Any]) -> str:
        """Computes canonical content hash of a sample for provenance tracking and holdout collision detection."""
        raw_content = (
            sample.get("text")
            or sample.get("raw_text")
            or (json.dumps(sample.get("tokenized_text", []), sort_keys=True) if sample.get("tokenized_text") else None)
            or sample.get("id")
            or json.dumps({k: v for k, v in sorted(sample.items()) if k not in ("_id", "timestamp")}, sort_keys=True)
        )
        norm = " ".join(str(raw_content).strip().lower().split())
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()

    def check_holdout_collision(
        self,
        samples: list[dict[str, Any]],
        protected_holdout_hashes: set[str],
    ) -> list[str]:
        """
        Verifies that none of the provided samples collide with protected holdout content hashes.
        Returns list of colliding sample hashes.
        """
        collisions = []
        for sample in samples:
            h = self.compute_sample_hash(sample)
            if h in protected_holdout_hashes:
                collisions.append(h)
        return collisions

    @staticmethod
    def fit_and_apply_normalization(
        splits: ManifestSplit,
        feature_keys: list[str],
    ) -> tuple[ManifestSplit, dict[str, dict[str, float]]]:
        """
        Fits normalization parameters (mean and standard deviation) strictly on train_samples,
        and applies the transformation across train, val, and test splits.
        Guarantees zero data leakage from validation or holdout test partitions.
        """
        import copy
        if not splits.train_samples:
            return splits, {}

        # 1. Fit strictly on train_samples
        stats: dict[str, dict[str, float]] = {}
        for key in feature_keys:
            vals = []
            for s in splits.train_samples:
                v = s.get(key)
                if isinstance(v, (int, float)):
                    vals.append(float(v))
            if vals:
                mean = sum(vals) / len(vals)
                variance = sum((x - mean) ** 2 for x in vals) / (len(vals) if len(vals) > 1 else 1)
                std = variance ** 0.5
                if std < 1e-8:
                    std = 1.0
                stats[key] = {"mean": mean, "std": std}

        # 2. Transform splits using train statistics
        def _transform_split(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
            transformed = []
            for s in samples:
                new_s = copy.deepcopy(s)
                for key, stat in stats.items():
                    if key in new_s and isinstance(new_s[key], (int, float)):
                        new_s[f"{key}_normalized"] = (float(new_s[key]) - stat["mean"]) / stat["std"]
                transformed.append(new_s)
            return transformed

        norm_splits = ManifestSplit(
            train_samples=_transform_split(splits.train_samples),
            val_samples=_transform_split(splits.val_samples),
            test_samples=_transform_split(splits.test_samples),
        )
        return norm_splits, stats

    def create_manifest_splits(
        self,
        samples: list[dict[str, Any]],
        train_ratio: float = 0.8,
        val_ratio: float = 0.1,
        test_ratio: float = 0.1,
        deduplicate: bool = True,
        embargo_hours: float = 0.0,
        purge_hours: float = 0.0,
    ) -> ManifestSplit:
        """
        Sorts samples strictly chronologically and partitions them into
        train, validation, and holdout test splits without lookahead bias.
        Optionally deduplicates identical content and applies temporal purge + embargo gaps.
        """
        if not samples:
            return ManifestSplit([], [], [])

        # Validate and parse timestamps, associating each with its sample
        stamped_samples: list[tuple[datetime.datetime, dict[str, Any]]] = []
        seen_content_hashes: set[str] = set()

        for sample in samples:
            dt = self._parse_timestamp(sample)

            if deduplicate:
                content_hash = self.compute_sample_hash(sample)
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

        if n == 1:
            raw_train = sorted_samples
            raw_val = []
            raw_test = []
        elif n == 2:
            raw_train = [sorted_samples[0]]
            raw_val = []
            raw_test = [sorted_samples[1]]
        else:
            train_end = min(max(int(n * train_ratio), 1), n - 2)
            val_end = min(max(int(n * (train_ratio + val_ratio)), train_end + 1), n - 1)
            raw_train = sorted_samples[:train_end]
            raw_val = sorted_samples[train_end:val_end]
            raw_test = sorted_samples[val_end:]

        total_gap = embargo_hours + purge_hours
        if total_gap > 0.0 and len(sorted_samples) >= 3:
            gap_delta = datetime.timedelta(hours=total_gap)
            train_end = min(max(int(n * train_ratio), 1), n - 2)
            raw_train = sorted_samples[:train_end]
            train_max_dt = self._parse_timestamp(raw_train[-1])

            # Samples available for validation after train_max_dt + total_gap
            avail_after_train = [s for s in sorted_samples[train_end:] if self._parse_timestamp(s) >= train_max_dt + gap_delta]
            if not avail_after_train:
                avail_after_train = [sorted_samples[-1]]

            n_avail = len(avail_after_train)
            if n_avail == 1:
                filtered_val = avail_after_train
                filtered_test = []
            else:
                target_val_count = max(int(n * val_ratio), 1)
                val_end = min(target_val_count, max(1, n_avail - 2))
                raw_val_cand = avail_after_train[:val_end]
                val_max_dt = self._parse_timestamp(raw_val_cand[-1])

                avail_after_val = [s for s in avail_after_train[val_end:] if self._parse_timestamp(s) >= val_max_dt + gap_delta]
                if not avail_after_val:
                    avail_after_val = [avail_after_train[-1]]

                filtered_val = raw_val_cand
                filtered_test = avail_after_val

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
        purge_hours: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Partitions samples, serializes JSONL files, computes independent per-split
        SHA-256 hashes as well as overall manifest hash, and writes manifest descriptor.
        Guarantees zero overlap with protected holdouts by content hash inspection.
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
            purge_hours=purge_hours,
        )

        # Protect holdouts by content and provenance: reject training if colliding with known holdouts
        if metadata and "protected_holdout_hashes" in metadata:
            prot_hashes = set(metadata["protected_holdout_hashes"])
            collisions = self.check_holdout_collision(samples, prot_hashes)
            if collisions:
                raise ValueError(
                    f"Dataset contains {len(collisions)} sample(s) that collide with protected holdout data: {collisions[:3]}. "
                    "Renaming or copying protected datasets is strictly prohibited."
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
        holdout_content_hashes = [self.compute_sample_hash(s) for s in splits.test_samples]

        is_holdout = bool((metadata or {}).get("is_holdout") or (metadata or {}).get("role") == "holdout")

        manifest_descriptor = {
            "manifest_id": manifest_id,
            "task": task,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sha256": sha256_hash,
            "total_samples": len(splits.train_samples) + len(splits.val_samples) + len(splits.test_samples),
            "is_holdout": is_holdout,
            "role": "holdout" if is_holdout else (metadata or {}).get("role", "training"),
            "provenance": (metadata or {}).get("provenance", {"role": "holdout" if is_holdout else "training"}),
            "holdout_content_hashes": holdout_content_hashes,
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
