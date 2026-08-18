import pytest
from unittest.mock import patch, AsyncMock

import httpx

from app.services.scraper_client import ScraperServiceClient


# The client POSTs to the standalone scraper-service over HTTP — mock at the
# httpx seam. (The old tests patched app.scraper.service, which the client no
# longer imports; test_scrape_success was silently doing a LIVE scrape.)
class _FakeResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


@pytest.fixture
def client():
    return ScraperServiceClient(base_url="http://scraper-test:8001")


@pytest.mark.asyncio
async def test_scrape_success(client):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _FakeResponse({
            "success": True,
            "content": "mocked article text",
            "url": "http://example.com",
            "engine_used": "playwright",
        })

        result = await client.scrape("http://example.com", engine="playwright")

        assert result["content"] == "mocked article text"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "http://scraper-test:8001/scrape"
        assert kwargs["json"]["url"] == "http://example.com"
        assert kwargs["json"]["engine"] == "playwright"


@pytest.mark.asyncio
async def test_scrape_failure(client):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _FakeResponse({"success": False, "error": "timeout"})
        result = await client.scrape("http://example.com")
        assert result is None
        assert client.failures == 0  # scraper answered; not an outage


@pytest.mark.asyncio
async def test_scrape_unreachable_counts_failure(client):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("connection refused")
        result = await client.scrape("http://example.com")
        assert result is None
        assert client.failures == 1
        assert "connection refused" in (client.last_error or "")


@pytest.mark.asyncio
async def test_collect_success(client):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _FakeResponse({
            "source": "reddit",
            "count": 2,
            "items": [{"id": "1"}, {"id": "2"}],
        })

        result = await client.collect("reddit", {"subreddits": ["wallstreetbets"]})

        assert len(result) == 2
        assert result[0]["id"] == "1"
        args, kwargs = mock_post.call_args
        assert args[0] == "http://scraper-test:8001/collect"
        assert kwargs["json"]["source"] == "reddit"
        assert kwargs["json"]["subreddits"] == ["wallstreetbets"]


@pytest.mark.asyncio
async def test_collect_error(client):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("boom")
        result = await client.collect("reddit", {"subreddits": ["wallstreetbets"]})
        assert result == []
        assert client.failures == 1
