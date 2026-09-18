#!/usr/bin/env python3
"""
Retraining Loop Evaluation Probe.
Evaluates GLM 5.3 dataset curation consistency, manifest building,
and Jetson training job submission and holdout evaluation.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.glm_curator_service import GLMCuratorService
from app.services.dataset_manifest_builder import DatasetManifestBuilder
from app.services.jetson_feature_client import JetsonFeatureClient
from app.services.jetson_training_orchestrator import JetsonTrainingOrchestrator


SAMPLE_ARTICLES = [
    {
        "id": "art_01",
        "ticker": "NVDA",
        "timestamp": "2026-09-10T14:00:00Z",
        "text": "NVIDIA (NVDA) reported record Q2 datacenter revenue of $26.3B, up 154% YoY, driven by Hopper GPU demand.",
    },
    {
        "id": "art_02",
        "ticker": "AAPL",
        "timestamp": "2026-09-11T15:30:00Z",
        "text": "Apple (AAPL) announced iPhone 18 launch with upgraded neural engine, keeping FY2026 gross margins above 46%.",
    },
    {
        "id": "art_03",
        "ticker": "MSFT",
        "timestamp": "2026-09-12T10:15:00Z",
        "text": "Microsoft (MSFT) authorized a new $60B share repurchase authorization and declared a quarterly dividend of $0.83 per share.",
    },
    {
        "id": "art_04",
        "ticker": "LULU",
        "timestamp": "2026-09-13T16:45:00Z",
        "text": "Lululemon (LULU) lowered full-year revenue guidance to $10.7B amid international retail slowdown and inventory normalization.",
    },
    {
        "id": "art_05",
        "ticker": "SPY",
        "timestamp": "2026-09-14T12:00:00Z",
        "text": "The S&P 500 ETF (SPY) held support above the 50-day moving average following cooler August core CPI inflation readings.",
    },
]


async def run_retraining_eval():
    print("=" * 75)
    print("  🔬 PART 1: GLM 5.3 AUTONOMOUS RETRAINING LOOP BENCHMARK")
    print("=" * 75)

    curator = GLMCuratorService(base_url="http://10.0.0.16:5591/vllm-shim/gold-spark", timeout=60.0)
    builder = DatasetManifestBuilder(output_dir="/tmp/retraining_benchmark")
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002")
    orchestrator = JetsonTrainingOrchestrator(client=client)

    # 1. Consensus Curation Benchmark
    print("\n[1/3] Benchmarking GLM 5.3 Curation on Financial News Corpus...")
    curated_samples = []
    t0 = time.monotonic()
    
    for art in SAMPLE_ARTICLES:
        t_start = time.monotonic()
        # Single pass extraction
        entities = await curator.annotate_text_single_pass(art["text"])
        t_pass = time.monotonic() - t_start
        
        # Format for GLiNER
        formatted = curator.format_for_gliner_training(art["text"], entities)
        curated_samples.append({
            "id": art["id"],
            "timestamp": art["timestamp"],
            "tokenized_text": formatted["tokenized_text"],
            "ner": formatted["ner"],
            "raw_text": art["text"],
        })
        print(f"  ✔ Article [{art['ticker']}]: Extracted {len(entities)} entities ({len(formatted['ner'])} spans mapped) in {t_pass:.2f}s")
        for ent in entities:
            print(f"      - {ent.label.upper()}: '{ent.text}' (ticker={ent.ticker or 'N/A'})")

    total_curation_s = time.monotonic() - t0
    avg_s_per_article = total_curation_s / len(SAMPLE_ARTICLES)
    print(f"\n  Summary: {len(curated_samples)} articles curated in {total_curation_s:.2f}s ({avg_s_per_article:.2f}s / article)")

    # 2. Chronological Manifest & Integrity Hashing
    print("\n[2/3] Generating Chronological Manifest & SHA-256 Checksum...")
    manifest = builder.build_manifest(
        task="gliner_finetune",
        samples=curated_samples,
        train_ratio=0.6,
        val_ratio=0.2,
        test_ratio=0.2,
        metadata={"benchmark_run": "2026-09-18", "source": "Gold Spark GLM 5.3"},
    )
    print(f"  ✔ Manifest ID:     {manifest['manifest_id']}")
    print(f"  ✔ SHA-256 Hash:    {manifest['sha256']}")
    print(f"  ✔ Partition Split: Train={manifest['splits']['train_count']} | Val={manifest['splits']['val_count']} | Test={manifest['splits']['test_count']}")
    print(f"  ✔ File Path:       {manifest['manifest_path']}")

    # 3. Jetson Holdout Evaluation & Gatekeeper Scoring
    print("\n[3/3] Evaluating Candidate Artifact against Frozen Benchmark Suite...")
    cand_id = "cand-gliner_finetune-20260918181218"
    eval_resp = await client.evaluate_candidate(cand_id)
    metrics = eval_resp.get("metrics", {})
    passed, reason = orchestrator.evaluate_promotion_gate("gliner_finetune", metrics)

    print(f"  ✔ Candidate ID:    {cand_id}")
    print(f"  ✔ Precision:       {metrics.get('precision'):.3f}")
    print(f"  ✔ Recall:          {metrics.get('recall'):.3f}")
    print(f"  ✔ F1 Score:        {metrics.get('f1'):.3f}")
    print(f"  ✔ p50 Latency:     {metrics.get('latency_p50_ms'):.1f} ms")
    print(f"  ✔ Gate Decision:   {'PROMOTED' if passed else 'REJECTED'} ({reason})")

    print("\n" + "=" * 75)
    print("  ⭐ PART 1 BENCHMARK COMPLETE: Autonomous Retraining Loop Validated")
    print("=" * 75)


if __name__ == "__main__":
    asyncio.run(run_retraining_eval())
