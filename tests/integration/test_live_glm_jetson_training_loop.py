"""
Live integration test for GLM 5.3 dataset curation and Jetson training loop.
Verifies end-to-end integration between Gold Spark (GLM 5.3) and Jetson Feature Platform.
"""

import tempfile
from pathlib import Path
import urllib.request
import pytest

from app.services.glm_curator_service import GLMCuratorService
from app.services.dataset_manifest_builder import DatasetManifestBuilder
from app.services.jetson_feature_client import JetsonFeatureClient


def _is_gold_spark_live() -> bool:
    try:
        with urllib.request.urlopen("http://10.0.0.16:5591/vllm-shim/gold-spark/v1/models", timeout=2.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def _is_jetson_live() -> bool:
    try:
        with urllib.request.urlopen("http://10.0.0.30:8002/health", timeout=2.0) as resp:
            return resp.status == 200
    except Exception:
        return False


GOLD_SPARK_AVAILABLE = _is_gold_spark_live()
JETSON_AVAILABLE = _is_jetson_live()


@pytest.mark.skipif(not GOLD_SPARK_AVAILABLE, reason="Gold Spark GLM 5.3 is not reachable")
@pytest.mark.asyncio
async def test_live_glm_curation_consensus(live_http):
    curator = GLMCuratorService(
        base_url="http://10.0.0.16:5591/vllm-shim/gold-spark",
        model_name="GLM-5.3-Flash-EXL3",
        timeout=60.0,
    )
    text = "Apple (AAPL) reported quarterly revenue of $85 billion, beating consensus expectations."
    
    # 1. Test live extraction from GLM 5.3 on Gold Spark
    single_res = await curator.annotate_text_single_pass(text)
    assert len(single_res) >= 1
    texts = [r.text.upper() for r in single_res]
    assert any("AAPL" in t or "APPLE" in t for t in texts)

    # 2. Test consensus filtering on live extracted entities
    consensus = curator.filter_consensus_spans([single_res, single_res], min_agreement=2)
    assert len(consensus) >= 1

    # 3. Test conversion to GLiNER training dataset format
    formatted = curator.format_for_gliner_training(text, consensus)
    assert "tokenized_text" in formatted
    assert "ner" in formatted
    assert len(formatted["ner"]) >= 1


@pytest.mark.skipif(not JETSON_AVAILABLE, reason="Jetson Feature Platform is not reachable")
@pytest.mark.asyncio
async def test_live_jetson_training_and_eval_routes(live_http):
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002")
    
    # 1. List existing jobs
    jobs = await client.list_training_jobs()
    assert isinstance(jobs, list)
    assert len(jobs) >= 1
    candidate_id = jobs[0].get("candidate_model_id")

    # 2. Evaluate candidate model on frozen suite
    if candidate_id:
        eval_res = await client.evaluate_candidate(candidate_id)
        assert eval_res.get("model_id") == candidate_id
        assert "metrics" in eval_res
        assert eval_res.get("gate_ready") is True
