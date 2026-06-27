import asyncio
import base64
import time
import httpx
from app.config import settings

# 1x1 Black PNG Base64
TEST_IMAGE_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="

async def benchmark_endpoint(name: str, url: str, model_id: str, prompt: str):
    print(f"\nEvaluating {name} ({model_id})...")
    completions_url = f"{url}/v1/chat/completions"
    
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{TEST_IMAGE_B64}"
                        }
                    }
                ]
            }
        ],
        "temperature": 0.2,
        "max_tokens": 128
    }

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(completions_url, json=payload)
            elapsed = time.monotonic() - start
            
            if resp.status_code != 200:
                print(f"❌ {name} failed with status {resp.status_code}: {resp.text}")
                return
                
            data = resp.json()
            message = data["choices"][0]["message"]
            response_text = message.get("content")
            
            if response_text is None:
                print(f"❌ {name} returned null content. Full response: {data}")
                return
                
            response_text = response_text.strip()
            
            # Estimate tokens
            tokens = len(response_text) // 4
            tps = tokens / elapsed if elapsed > 0 else 0
            
            print(f"✅ {name} Success ({elapsed:.2f}s, ~{tps:.1f} tok/s)")
            print(f"   Response: {response_text}")
    except Exception as e:
        print(f"❌ {name} failed: {e}")

async def main():
    print("=" * 60)
    print("Local vLLM Vision Endpoint Benchmark")
    print("=" * 60)
    
    prompt = "What color is this image? Output just the color name."
    
    await asyncio.gather(
        benchmark_endpoint(
            "Jetson (Qwen)",
            settings.PROVIDER_VLLM_1_URL,
            "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit",
            prompt
        ),
        benchmark_endpoint(
            "Gold Spark (Gemma)",
            settings.PROVIDER_VLLM_2_URL,
            "google/gemma-4-26B-A4B-it",
            prompt
        )
    )

if __name__ == "__main__":
    asyncio.run(main())
