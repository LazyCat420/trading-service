import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from app.tools.vllm_vision_tools import vllm_vision_analyze

@pytest.mark.asyncio
async def test_vllm_vision_analyze_local_file_missing():
    # Test file path that doesn't exist
    result = await vllm_vision_analyze(
        prompt="Describe this",
        image_path_or_url="/invalid/path/to/image.png",
        model_name="google/gemma-4-26B-A4B-it"
    )
    assert "error" in result
    assert "path does not exist" in result.get("error", "")

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
@patch("builtins.open")
@patch("os.path.exists")
async def test_vllm_vision_analyze_success(mock_exists, mock_open, mock_post):
    mock_exists.return_value = True
    
    # Mock file reading (synchronous open)
    mock_file = MagicMock()
    mock_file.read.return_value = b"fake_image_bytes"
    
    # MagicMock for context manager
    mock_context = MagicMock()
    mock_context.__enter__.return_value = mock_file
    mock_open.return_value = mock_context
    
    # Mock HTTP response (asynchronous post)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "This is a mocked description."
                }
            }
        ]
    }
    
    # We must patch AsyncClient.post to be an AsyncMock that returns our mock response
    mock_post.return_value = mock_resp
    
    result = await vllm_vision_analyze(
        prompt="Describe this",
        image_path_or_url="/home/lazycat/github/projects/sun/test.png",
        model_name="google/gemma-4-26B-A4B-it"
    )
    
    assert result.get("status") == "success"
    assert "mocked description" in result.get("analysis", "")
    assert result.get("model_used") == "google/gemma-4-26B-A4B-it"
    
    # Verify post was called with correct structure
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    payload = kwargs["json"]
    assert payload["model"] == "google/gemma-4-26B-A4B-it"
    assert payload["messages"][0]["content"][0]["text"] == "Describe this"
    assert "base64" in payload["messages"][0]["content"][1]["image_url"]["url"]
