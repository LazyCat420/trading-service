import os
import aiohttp
import logging
from pydantic import BaseModel, Field
from app.tools.registry import registry, PermissionLevel

logger = logging.getLogger(__name__)

class LocateAnythingInput(BaseModel):
    image_url: str = Field(..., description="The URL or base64 data of the image.")
    prompt: str = Field(..., description="The description of what to find in the image (e.g., 'the largest red car', 'a login button').")

@registry.register(
    name="locate_anything",
    description="Locates an object, text, or UI element in an image using natural language via the LocateAnything VLM model. Returns bounding box coordinates.",
    tier=1,
    source="locate_anything",
    permission=PermissionLevel.READ_ONLY,
    input_model=LocateAnythingInput,
    parameters=LocateAnythingInput.model_json_schema()
)
async def locate_anything(image_url: str, prompt: str, **kwargs) -> dict:
    """
    Locates an object, text, or UI element in an image using natural language.
    """
    api_url = os.environ.get("LOCATE_ANYTHING_API_URL")
    if not api_url:
        return {
            "error": "LOCATE_ANYTHING_API_URL environment variable is not set. Please deploy a llama.cpp or locate-anything.cpp server and set this variable."
        }
    
    try:
        payload = {
            "prompt": prompt,
            "image_url": image_url
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{api_url}/locate", json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    text = await response.text()
                    return {"error": f"API returned status {response.status}", "details": text}
    except Exception as e:
        logger.error(f"Error calling LocateAnything API: {str(e)}")
        return {"error": str(e)}
