"""Discovery for the independently packaged scraper; no trading-service imports."""
import os
import httpx


async def discover_purge_model():
    # Endpoint configuration is permitted; model IDs always come from the box.
    url = os.getenv("PROVIDER_VLLM_1_URL")
    if not url:
        raise RuntimeError("PROVIDER_VLLM_1_URL is required for LLM purge filtering")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url.rstrip("/") + "/v1/models")
        response.raise_for_status()
        models = [m for m in response.json().get("data", [])
                  if m.get("id") and m.get("task", "generate") not in ("embed", "embedding", "rerank")]
    if len(models) != 1:
        raise RuntimeError("Purge endpoint must advertise one unambiguous generation model")
    return models[0]["id"], "vllm"
