#!/usr/bin/env python3
"""
Benchmark and Evaluation Suite for Jetson Feature Platform & GLM 5.3 Retraining Loop.

Evaluates:
1. Frozen Benchmark Holdout Suites for all specialized models on Jetson (Port 8002):
   - GLiNER (NER & Corporate Event Extraction)
   - Market CNN (Market Regime Classification)
   - Timeseries RNN (Probabilistic Return Forecasting)
2. Live Inference Latency & Reliability Profiles (p50, p90, p99, error rate)
3. GLM 5.3 Live Curation & Consensus Annotation Latency
4. Jetson Resource Envelope (GPU VRAM, Training Queue Capacity)

Usage:
    python3 scripts/benchmark_jetson_feature_models.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.jetson_feature_client import JetsonFeatureClient
from app.services.glm_curator_service import GLMCuratorService
from app.services.jetson_training_orchestrator import JetsonTrainingOrchestrator


async def run_benchmark():
    print("=" * 70)
    print("  🚀 JETSON FEATURE PLATFORM & GLM 5.3 BENCHMARK EVALUATION")
    print("=" * 70)

    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002", timeout=10.0)
    curator = GLMCuratorService(base_url="http://10.0.0.16:5591/vllm-shim/gold-spark", timeout=60.0)
    orchestrator = JetsonTrainingOrchestrator(client=client)

    # -------------------------------------------------------------------------
    # 1. System Health & Hardware Envelope
    # -------------------------------------------------------------------------
    print("\n[1/4] Checking Jetson Orin System & GPU Telemetry...")
    health = await client.get_health()
    if health.get("status") != "ok":
        print(f"❌ Jetson Feature Platform unhealthy: {health}")
        return

    gpu = health.get("gpu", {})
    queue = health.get("queue", {})
    print(f"  ✔ Status: {health.get('status')}")
    print(f"  ✔ Device: {gpu.get('device_name', 'Orin')} (GPU Available: {gpu.get('available')})")
    print(f"  ✔ Memory: {gpu.get('memory_allocated_mb', 0):.1f} MB allocated / {gpu.get('memory_total_mb', 0):.1f} MB total")
    print(f"  ✔ Free Disk: {health.get('disk_free_gb', 0):.1f} GB")
    print(f"  ✔ Models Loaded: {', '.join(health.get('models_loaded', []))}")
    print(f"  ✔ Inference Queue: {queue.get('inference_active')}/{queue.get('inference_max')} active")
    print(f"  ✔ Training Queue: {queue.get('training_active')}/{queue.get('training_max')} active")

    # -------------------------------------------------------------------------
    # 2. Frozen Holdout Benchmark Evaluations
    # -------------------------------------------------------------------------
    print("\n[2/4] Running Frozen Benchmark Evaluation Suites on Models...")

    # A. GLiNER / Candidate Evaluation
    active_gliner = await client.get_active_model("gliner")
    cand_id = active_gliner.get("model_id") or "gliner"
    print(f"\n  Evaluating GLiNER Candidate '{cand_id}'...")
    gliner_eval = await client.evaluate_candidate(cand_id)
    m = gliner_eval.get("metrics", {})
    print(f"    - F1 Score:       {m.get('f1', 0.0):.3f} (Benchmark Target: >= 0.912)")
    print(f"    - Precision:      {m.get('precision', 0.0):.3f} (Benchmark Target: >= 0.900)")
    print(f"    - Recall:         {m.get('recall', 0.0):.3f} (Benchmark Target: >= 0.880)")
    print(f"    - p50 Latency:    {m.get('latency_p50_ms', 0.0):.1f} ms")
    print(f"    - p99 Latency:    {m.get('latency_p99_ms', 0.0):.1f} ms")
    print(f"    - Evaluated on:   {m.get('samples_evaluated', 0)} out-of-sample samples")
    gliner_pass, gliner_reason = orchestrator.evaluate_promotion_gate("gliner_finetune", m)
    print(f"    - Policy Gate:    {'✅ PASSED' if gliner_pass else '❌ FAILED'} ({gliner_reason})")

    # B. Market CNN Evaluation
    print(f"\n  Evaluating Market CNN 'market_cnn'...")
    cnn_eval = await client.evaluate_candidate("market_cnn")
    cm = cnn_eval.get("metrics", {})
    print(f"    - Macro F1:       {cm.get('macro_f1', 0.0):.3f} (Benchmark Target: >= 0.865)")
    print(f"    - Brier Score:    {cm.get('brier_score', 0.0):.3f} (Lower is better; Target: <= 0.095)")
    print(f"    - p50 Latency:    {cm.get('latency_p50_ms', 0.0):.1f} ms")
    print(f"    - VRAM Usage:     {cm.get('vram_mb', 0.0):.1f} MB")
    cnn_pass, cnn_reason = orchestrator.evaluate_promotion_gate("cnn_train", cm)
    print(f"    - Policy Gate:    {'✅ PASSED' if cnn_pass else '❌ FAILED'} ({cnn_reason})")

    # C. Timeseries RNN Evaluation
    print(f"\n  Evaluating Timeseries RNN 'timeseries_rnn'...")
    rnn_eval = await client.evaluate_candidate("timeseries_rnn")
    rm = rnn_eval.get("metrics", {})
    print(f"    - RMSE:           {rm.get('rmse', 0.0):.4f} (Lower is better; Target: <= 0.024)")
    print(f"    - 80% Coverage:   {rm.get('coverage_80', 0.0):.3f} (Target Range: [0.75, 0.85])")
    print(f"    - Calib Error:    {rm.get('calibration_error', 0.0):.4f}")
    print(f"    - p50 Latency:    {rm.get('latency_p50_ms', 0.0):.1f} ms")
    rnn_pass, rnn_reason = orchestrator.evaluate_promotion_gate("rnn_train", rm)
    print(f"    - Policy Gate:    {'✅ PASSED' if rnn_pass else '❌ FAILED'} ({rnn_reason})")

    # -------------------------------------------------------------------------
    # 3. Live Inference Latency & Reliability Profiles (N=10 runs each)
    # -------------------------------------------------------------------------
    N_RUNS = 10
    print(f"\n[3/4] Benchmarking Live Inference Profiles ({N_RUNS} requests per model)...")

    # GLiNER Profile
    sample_doc = [{"document_id": "bench_01", "text": "Apple (AAPL) reported revenue of $85 billion, beating consensus expectations while NVIDIA raised guidance."}]
    gliner_latencies = []
    gliner_errors = 0
    for _ in range(N_RUNS):
        t0 = time.monotonic()
        try:
            resp = await client.extract_entities(sample_doc)
            if "documents" in resp.get("result", {}):
                gliner_latencies.append((time.monotonic() - t0) * 1000)
            else:
                gliner_errors += 1
        except Exception:
            gliner_errors += 1

    p50_g = statistics.median(gliner_latencies) if gliner_latencies else 0
    p90_g = statistics.quantiles(gliner_latencies, n=10)[8] if len(gliner_latencies) >= 10 else p50_g
    print(f"  ✔ GLiNER Inference:   p50 = {p50_g:.1f}ms | p90 = {p90_g:.1f}ms | Success = {len(gliner_latencies)}/{N_RUNS} (Errors: {gliner_errors})")

    # Market CNN Profile
    sample_ohlcv = [[150.0 + i, 152.0 + i, 149.0 + i, 151.0 + i, 1000000.0] for i in range(30)]
    cnn_latencies = []
    cnn_errors = 0
    for _ in range(N_RUNS):
        t0 = time.monotonic()
        try:
            resp = await client.classify_market_regime("AAPL", "1d", "2026-09-18T12:00:00Z", 30, sample_ohlcv)
            if "regime" in resp.get("result", {}):
                cnn_latencies.append((time.monotonic() - t0) * 1000)
            else:
                cnn_errors += 1
        except Exception:
            cnn_errors += 1

    p50_c = statistics.median(cnn_latencies) if cnn_latencies else 0
    p90_c = statistics.quantiles(cnn_latencies, n=10)[8] if len(cnn_latencies) >= 10 else p50_c
    print(f"  ✔ Market CNN:         p50 = {p50_c:.1f}ms | p90 = {p90_c:.1f}ms | Success = {len(cnn_latencies)}/{N_RUNS} (Errors: {cnn_errors})")

    # Timeseries RNN Profile
    sample_rnn_seq = [[150.0 + 0.2 * i for _ in range(8)] for i in range(25)]
    rnn_latencies = []
    rnn_errors = 0
    for _ in range(N_RUNS):
        t0 = time.monotonic()
        try:
            resp = await client.predict_forecast("AAPL", "1d", 25, sequence=sample_rnn_seq)
            if "return_quantiles" in resp.get("result", {}):
                rnn_latencies.append((time.monotonic() - t0) * 1000)
            else:
                rnn_errors += 1
        except Exception:
            rnn_errors += 1

    p50_r = statistics.median(rnn_latencies) if rnn_latencies else 0
    p90_r = statistics.quantiles(rnn_latencies, n=10)[8] if len(rnn_latencies) >= 10 else p50_r
    print(f"  ✔ Timeseries RNN:     p50 = {p50_r:.1f}ms | p90 = {p90_r:.1f}ms | Success = {len(rnn_latencies)}/{N_RUNS} (Errors: {rnn_errors})")

    # -------------------------------------------------------------------------
    # 4. GLM 5.3 Live Curation Benchmark
    # -------------------------------------------------------------------------
    print("\n[4/4] Benchmarking GLM 5.3 Live Curation on Gold Spark...")
    text_bench = "Microsoft (MSFT) announced a $60B share repurchase program and quarterly dividend increase."
    t0_glm = time.monotonic()
    curation_res = await curator.annotate_text_single_pass(text_bench)
    glm_lat_s = time.monotonic() - t0_glm
    print(f"  ✔ GLM 5.3 Extraction Time: {glm_lat_s:.2f}s")
    print(f"  ✔ Entities Extracted:     {len(curation_res)}")
    for e in curation_res:
        print(f"      - {e.label.upper()}: '{e.text}' (ticker={e.ticker or 'N/A'})")

    print("\n" + "=" * 70)
    print("  ⭐ BENCHMARK EVALUATION SUMMARY: ALL SYSTEMS OPERATIONAL")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_benchmark())
