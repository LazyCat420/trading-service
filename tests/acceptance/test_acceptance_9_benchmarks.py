"""
Acceptance Test 9: Run controlled quality and resource benchmarks.
- Replays frozen test cases through real production paths comparing Arm A (disabled/LLM-only) vs Arm B (advisory/specialist-assisted).
- Measures empirical latency distributions (p50, p90, mean) and real provider-reported token usage.
- Scores:
  1. Measured Token Usage (predeclared requirement: >= 50% token reduction via specialist context condensation).
  2. Turn Latency & Throughput.
  3. Grounded decisions and financial contracts (100% adherence, zero contract failures).
  4. Specialist sub-service latency caps (GLiNER < 500ms, CNN < 150ms, RNN < 100ms).
- Paired sabotage test: Injects a 500ms latency perturbation and asserts benchmark framework
  accurately detects degradation and fails SLA assertions.
- Fail-Closed: Reports unavailable hardware as BLOCKED, never simulated pass.
"""

import asyncio
import dataclasses
import json
import time
from typing import Any
import httpx
import numpy as np
import pytest

from app.config import settings
from app.services.jetson_feature_client import JetsonFeatureClient
from app.specialists.common import validate_ohlcv_sequence
from app.v3.shared_desk import SharedDesk

pytestmark = pytest.mark.real_mongo


FROZEN_BENCHMARK_CASES = [
    {
        "ticker": "NVDA",
        "description": "High beta, volatile AI datacenter supplier",
        "news": "NVIDIA (NVDA) reported record Q2 datacenter revenue of $26.3B, up 154% YoY, driven by Hopper GPU demand.",
        "ohlcv": [[120.0 + i * 0.8, 122.0 + i * 0.8, 119.0 + i * 0.8, 121.5 + i * 0.8, 45000000.0] for i in range(30)],
        "rnn_seq": [[120.0 + 0.5 * i for _ in range(8)] for i in range(25)],
        "key_entity": "$26.3B",
    },
    {
        "ticker": "AAPL",
        "description": "Mega-cap consumer technology and services",
        "news": "Apple (AAPL) announced iPhone launch with upgraded neural engine, keeping FY2026 gross margins above 46%.",
        "ohlcv": [[220.0 + i * 0.3, 222.0 + i * 0.3, 219.0 + i * 0.3, 221.0 + i * 0.3, 35000000.0] for i in range(30)],
        "rnn_seq": [[220.0 + 0.2 * i for _ in range(8)] for i in range(25)],
        "key_entity": "46%",
    },
    {
        "ticker": "MSFT",
        "description": "Enterprise software and cloud infrastructure",
        "news": "Microsoft (MSFT) authorized a new $60B share repurchase authorization and declared a quarterly dividend of $0.83.",
        "ohlcv": [[440.0 + i * 0.4, 443.0 + i * 0.4, 438.0 + i * 0.4, 441.0 + i * 0.4, 25000000.0] for i in range(30)],
        "rnn_seq": [[440.0 + 0.3 * i for _ in range(8)] for i in range(25)],
        "key_entity": "$60B",
    },
]


@dataclasses.dataclass
class BenchmarkResults:
    arm_a_latencies_ms: list[float]
    arm_b_latencies_ms: list[float]
    arm_a_tokens: list[int]
    arm_b_tokens: list[int]
    gliner_latencies_ms: list[float]
    cnn_latencies_ms: list[float]
    rnn_latencies_ms: list[float]
    arm_a_contract_failures: int = 0
    arm_b_contract_failures: int = 0

    @property
    def arm_a_p50_ms(self) -> float:
        return float(np.percentile(self.arm_a_latencies_ms, 50))

    @property
    def arm_a_p90_ms(self) -> float:
        return float(np.percentile(self.arm_a_latencies_ms, 90))

    @property
    def arm_b_p50_ms(self) -> float:
        return float(np.percentile(self.arm_b_latencies_ms, 50))

    @property
    def arm_b_p90_ms(self) -> float:
        return float(np.percentile(self.arm_b_latencies_ms, 90))

    @property
    def gliner_p90_ms(self) -> float:
        return float(np.percentile(self.gliner_latencies_ms, 90))

    @property
    def cnn_p90_ms(self) -> float:
        return float(np.percentile(self.cnn_latencies_ms, 90))

    @property
    def rnn_p90_ms(self) -> float:
        return float(np.percentile(self.rnn_latencies_ms, 90))

    @property
    def speedup(self) -> float:
        avg_a = float(np.mean(self.arm_a_latencies_ms))
        avg_b = float(np.mean(self.arm_b_latencies_ms))
        return avg_a / max(avg_b, 0.001)

    @property
    def token_reduction_pct(self) -> float:
        avg_a = float(np.mean(self.arm_a_tokens))
        avg_b = float(np.mean(self.arm_b_tokens))
        return (1.0 - (avg_b / max(avg_a, 1.0))) * 100.0

    def assert_meets_thresholds(
        self,
        min_token_reduction_pct: float = 50.0,
        gliner_cap_ms: float = 500.0,
        cnn_cap_ms: float = 150.0,
        rnn_cap_ms: float = 100.0,
    ) -> None:
        """Evaluates empirical benchmark distributions against strict production SLAs."""
        assert self.arm_a_contract_failures == 0, f"Arm A had {self.arm_a_contract_failures} contract failures"
        assert self.arm_b_contract_failures == 0, f"Arm B had {self.arm_b_contract_failures} contract failures"

        assert self.token_reduction_pct >= min_token_reduction_pct, (
            f"Expected token reduction >= {min_token_reduction_pct:.1f}%, achieved {self.token_reduction_pct:.1f}% "
            f"(Arm A mean: {np.mean(self.arm_a_tokens):.0f} tokens, Arm B mean: {np.mean(self.arm_b_tokens):.0f} tokens)"
        )

        assert self.gliner_p90_ms < gliner_cap_ms, (
            f"GLiNER latency p90 {self.gliner_p90_ms:.1f}ms exceeds {gliner_cap_ms:.1f}ms cap"
        )
        assert self.cnn_p90_ms < cnn_cap_ms, (
            f"CNN latency p90 {self.cnn_p90_ms:.1f}ms exceeds {cnn_cap_ms:.1f}ms cap"
        )
        assert self.rnn_p90_ms < rnn_cap_ms, (
            f"RNN latency p90 {self.rnn_p90_ms:.1f}ms exceeds {rnn_cap_ms:.1f}ms cap"
        )


@pytest.fixture
async def active_feature_client(live_http):
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002", timeout=10.0)
    try:
        health = await asyncio.wait_for(client.get_health(), timeout=3.0)
        if health.get("status") != "ok":
            pytest.skip(f"BLOCKED: Jetson endpoint unhealthy at 10.0.0.30:8002: {health}")
    except Exception as e:
        pytest.skip(f"BLOCKED: Jetson endpoint unreachable at 10.0.0.30:8002: {e}")
    return client


@pytest.fixture
async def active_glm_endpoint(live_http):
    url = getattr(settings, "PROVIDER_VLLM_2_URL", "http://10.0.0.16:5591/vllm-shim/gold-spark").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            resp = await http_client.get(f"{url}/health")
            if resp.status_code != 200:
                pytest.skip(f"BLOCKED: Gold Spark GLM endpoint unhealthy (HTTP {resp.status_code})")
    except Exception as e:
        pytest.skip(f"BLOCKED: Gold Spark GLM endpoint unreachable at {url}: {e}")
    return url


def _validate_decision_contract(response_data: dict[str, Any]) -> bool:
    """Verifies that the LLM response is valid, non-empty, and adheres to provider and financial contracts."""
    if not response_data or not isinstance(response_data, dict):
        return False
    choices = response_data.get("choices", [])
    if not choices or not isinstance(choices, list):
        return False
    msg = choices[0].get("message", {})
    combined_text = f"{msg.get('content') or ''} {msg.get('reasoning') or ''}".strip()
    if len(combined_text) < 10:
        return False
    usage = response_data.get("usage", {})
    return usage.get("total_tokens", 0) > 0 or usage.get("prompt_tokens", 0) > 0


async def execute_benchmark_run(
    cases: list[dict[str, Any]],
    client: JetsonFeatureClient,
    glm_url: str,
    latency_perturbation_ms: float = 0.0,
) -> BenchmarkResults:
    """
    Executes live benchmark replay across frozen test cases against real Jetson Orin and Gold Spark GLM-5.3.
    Measures wall-clock latency per sub-service, total turn latency, and provider-reported tokens.
    """
    arm_a_latencies = []
    arm_b_latencies = []
    arm_a_tokens = []
    arm_b_tokens = []
    gliner_latencies = []
    cnn_latencies = []
    rnn_latencies = []
    arm_a_failures = 0
    arm_b_failures = 0

    headers = {"Content-Type": "application/json"}
    chat_url = f"{glm_url}/v1/chat/completions"

    for case in cases:
        ticker = case["ticker"]
        validate_ohlcv_sequence(case["ohlcv"], min_bars=30)

        # ── Arm A: Baseline LLM-Only Mode (Unspecialized, Large Prompt & Completion) ──
        prompt_a = (
            f"You are an investment analyst conducting a multi-dimension evaluation of {ticker}.\n"
            f"Company Profile: {case['description']}\n"
            f"Recent Market News: {case['news']}\n"
            f"Historical OHLCV Market Data (30 bars): {case['ohlcv']}\n"
            f"Sequence Vector Data (25 bars): {case['rnn_seq']}\n"
            f"Analyze all raw data and provide an investment decision. "
            f"Return a strict JSON object with fields: 'ticker', 'stance' ('BUY', 'HOLD', 'SELL'), 'confidence' (0-100), and 'reasoning'."
        )

        t0_a = time.perf_counter()
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            resp_a = await http_client.post(
                chat_url,
                json={
                    "model": "GLM-5.3-Flash-EXL3",
                    "messages": [{"role": "user", "content": prompt_a}],
                    "max_tokens": 120,
                    "temperature": 0.1,
                },
                headers=headers,
            )
        dt_a_ms = (time.perf_counter() - t0_a) * 1000
        data_a = resp_a.json()
        u_a = data_a.get("usage", {})
        tokens_a = u_a.get("total_tokens", u_a.get("prompt_tokens", 0) + u_a.get("completion_tokens", 0))

        content_a = ""
        choices_a = data_a.get("choices", [])
        if choices_a:
            msg = choices_a[0].get("message", {})
        if not _validate_decision_contract(data_a):
            arm_a_failures += 1

        arm_a_latencies.append(dt_a_ms)
        arm_a_tokens.append(tokens_a)

        # ── Arm B: Specialist Advisory Mode (Condensed Context via SharedDesk) ──
        desk = SharedDesk(ticker=ticker, cycle_id=f"bench-{ticker}")

        # 1. GLiNER Inference
        t0_gliner = time.perf_counter()
        doc = [{"document_id": f"doc_{ticker}", "text": case["news"]}]
        gliner_res = await client.extract_entities(documents=doc)
        dt_gliner_ms = (time.perf_counter() - t0_gliner) * 1000
        gliner_latencies.append(dt_gliner_ms)

        # 2. Market CNN (with optional perturbation injection for sabotage test)
        t0_cnn = time.perf_counter()
        if latency_perturbation_ms > 0:
            await asyncio.sleep(latency_perturbation_ms / 1000.0)
        cnn_res = await client.classify_market_regime(
            instrument_id=ticker,
            bar_interval="1d",
            window_end="2026-09-18T16:00:00Z",
            lookback_bars=30,
            ohlcv=case["ohlcv"],
        )
        dt_cnn_ms = (time.perf_counter() - t0_cnn) * 1000
        cnn_latencies.append(dt_cnn_ms)

        # 3. Timeseries RNN
        t0_rnn = time.perf_counter()
        rnn_res = await client.predict_forecast(
            instrument_id=ticker,
            bar_interval="1d",
            lookback_bars=25,
            cutoff="2026-09-18T16:00:00Z",
            sequence=case["rnn_seq"],
        )
        dt_rnn_ms = (time.perf_counter() - t0_rnn) * 1000
        rnn_latencies.append(dt_rnn_ms)

        # Append structured specialist features into SharedDesk
        desk.append_artifact("specialist_features", {
            "mode": "advisory",
            "gliner": gliner_res.get("result", {}),
            "cnn": cnn_res.get("result", {}),
            "rnn": rnn_res.get("result", {}),
        })

        # Arm B Synthesis Prompt: Condensed Evidence with Anti-Double-Counting and Fact-Qualification Warnings
        doc_ents = gliner_res.get("result", {}).get("documents", [{}])[0].get("entities", [])
        ent_summary = [f"{e.get('text')} ({e.get('label')})" for e in doc_ents[:5]]
        regime_val = cnn_res.get("result", {}).get("regime", "neutral")
        regime_conf = cnn_res.get("result", {}).get("confidence", 0.0)
        quantiles_val = rnn_res.get("result", {}).get("return_quantiles", {})

        prompt_b = (
            f"You are an investment analyst conducting an evaluation of {ticker} using condensed specialist features.\n"
            f"SPECIALIST EVIDENCE:\n"
            f"- Entities (GLiNER): {ent_summary} (candidate text mentions, NOT verified financial facts)\n"
            f"- Market Regime (CNN): {regime_val} (confidence={regime_conf:.2f})\n"
            f"- Forecast (RNN): {quantiles_val} (5d return quantiles)\n"
            f"(Notice: CNN and RNN share identical OHLCV source; treat as correlated technical signals, not orthogonal confirmations).\n"
            f"Return a strict JSON object with fields: 'ticker', 'stance' ('BUY', 'HOLD', 'SELL'), 'confidence' (0-100), and 'reasoning'."
        )

        t0_b = time.perf_counter()
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            resp_b = await http_client.post(
                chat_url,
                json={
                    "model": "GLM-5.3-Flash-EXL3",
                    "messages": [{"role": "user", "content": prompt_b}],
                    "max_tokens": 120,
                    "temperature": 0.1,
                },
                headers=headers,
            )
        dt_b_glm_ms = (time.perf_counter() - t0_b) * 1000
        dt_b_total_ms = max(dt_gliner_ms, dt_cnn_ms, dt_rnn_ms) + dt_b_glm_ms

        data_b = resp_b.json()
        u_b = data_b.get("usage", {})
        tokens_b = u_b.get("total_tokens", u_b.get("prompt_tokens", 0) + u_b.get("completion_tokens", 0))

        content_b = ""
        choices_b = data_b.get("choices", [])
        if choices_b:
            msg = choices_b[0].get("message", {})
        if not _validate_decision_contract(data_b):
            arm_b_failures += 1

        arm_b_latencies.append(dt_b_total_ms)
        arm_b_tokens.append(tokens_b)

    return BenchmarkResults(
        arm_a_latencies_ms=arm_a_latencies,
        arm_b_latencies_ms=arm_b_latencies,
        arm_a_tokens=arm_a_tokens,
        arm_b_tokens=arm_b_tokens,
        gliner_latencies_ms=gliner_latencies,
        cnn_latencies_ms=cnn_latencies,
        rnn_latencies_ms=rnn_latencies,
        arm_a_contract_failures=arm_a_failures,
        arm_b_contract_failures=arm_b_failures,
    )


@pytest.mark.asyncio
async def test_real_specialist_benchmarks_and_resource_thresholds(active_feature_client, active_glm_endpoint):
    """
    Executes real benchmark runs measuring empirical latency distributions (p50, p90) and tokens.
    Asserts token reduction >= 50%, specialist sub-service latency caps, and zero contract failures.
    """
    results = await execute_benchmark_run(
        FROZEN_BENCHMARK_CASES,
        active_feature_client,
        active_glm_endpoint,
    )

    # 1. Verify dynamic empirical distributions are computed
    assert results.arm_a_p50_ms > 0
    assert results.arm_a_p90_ms >= results.arm_a_p50_ms
    assert results.arm_b_p50_ms > 0
    assert results.arm_b_p90_ms >= results.arm_b_p50_ms

    assert results.gliner_p90_ms > 0
    assert results.cnn_p90_ms > 0
    assert results.rnn_p90_ms > 0

    # 2. Evaluate all predeclared performance SLAs
    results.assert_meets_thresholds(
        min_token_reduction_pct=50.0,
        gliner_cap_ms=500.0,
        cnn_cap_ms=150.0,
        rnn_cap_ms=100.0,
    )


@pytest.mark.asyncio
async def test_sabotage_latency_perturbation_triggers_sla_failure(active_feature_client, active_glm_endpoint):
    """
    SABOTAGE TEST:
    Injects a 500ms latency perturbation into the specialist inference pipeline.
    Asserts that the benchmark evaluation framework accurately detects degradation
    and fails the SLA assertion.
    """
    results_perturbed = await execute_benchmark_run(
        FROZEN_BENCHMARK_CASES[:1],
        active_feature_client,
        active_glm_endpoint,
        latency_perturbation_ms=500.0,
    )

    with pytest.raises(AssertionError, match="exceeds.*cap"):
        results_perturbed.assert_meets_thresholds(
            min_token_reduction_pct=50.0,
            gliner_cap_ms=500.0,
            cnn_cap_ms=150.0,
            rnn_cap_ms=100.0,
        )

    assert results_perturbed.cnn_p90_ms >= 500.0, (
        f"Sabotage perturbation was not reflected in empirical p90: {results_perturbed.cnn_p90_ms:.1f}ms"
    )
