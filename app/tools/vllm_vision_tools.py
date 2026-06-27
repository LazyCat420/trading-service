import base64
import os
import httpx
from pydantic import BaseModel, Field
from app.tools.registry import registry
from app.config import settings

class VLLMVisionInput(BaseModel):
    prompt: str = Field(..., description="The textual question or instruction for the vision model.")
    image_path_or_url: str = Field(..., description="Local absolute file path or remote HTTP URL of the image to analyze.")
    model_name: str = Field("google/gemma-4-26B-A4B-it", description="The vision model to use. Options: 'google/gemma-4-26B-A4B-it' (default, Gold Spark) or 'cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit' (Jetson).")

@registry.register(
    name="vllm_vision_analyze",
    description="Analyze an image (from a local file path or remote URL) using a local vLLM vision model (Gemma 4 or Qwen 3.6).",
    input_model=VLLMVisionInput
)
async def vllm_vision_analyze(prompt: str, image_path_or_url: str, model_name: str = "google/gemma-4-26B-A4B-it") -> dict:
    # 1. Resolve endpoint URL
    if "qwen" in model_name.lower():
        endpoint = settings.PROVIDER_VLLM_1_URL
        actual_model = "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"
    else:
        endpoint = settings.PROVIDER_VLLM_2_URL
        actual_model = "google/gemma-4-26B-A4B-it"

    # 2. Get image data and base64 encode it
    try:
        if image_path_or_url.startswith("http://") or image_path_or_url.startswith("https://"):
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(image_path_or_url)
                resp.raise_for_status()
                img_bytes = resp.content
        else:
            if not os.path.isabs(image_path_or_url):
                # If relative, look inside the workspace root
                workspace_root = "/home/lazycat/github/projects/sun"
                resolved_path = os.path.join(workspace_root, image_path_or_url)
            else:
                resolved_path = image_path_or_url
                
            if not os.path.exists(resolved_path):
                return {"error": f"Local image path does not exist: {resolved_path}"}
                
            with open(resolved_path, "rb") as f:
                img_bytes = f.read()
                
        base64_data = base64.b64encode(img_bytes).decode("utf-8")
    except Exception as e:
        return {"error": f"Failed to read/download image: {str(e)}"}

    # 3. Call vLLM endpoint
    url = f"{endpoint}/v1/chat/completions"
    
    # Multimodal format for OpenAI/vLLM API
    payload = {
        "model": actual_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_data}"
                        }
                    }
                ]
            }
        ],
        "temperature": 0.2,
        "max_tokens": 1024
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                return {"error": f"vLLM API returned status {resp.status_code}: {resp.text}"}
            
            result = resp.json()
            analysis = result["choices"][0]["message"]["content"]
            return {
                "status": "success",
                "model_used": actual_model,
                "analysis": analysis
            }
    except Exception as e:
        return {"error": f"HTTP request to vLLM failed: {str(e)}"}
