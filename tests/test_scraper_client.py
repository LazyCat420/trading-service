import pytest
from unittest.mock import patch, AsyncMock

from app.services.scraper_client import scraper_client, ScraperServiceClient


@pytest.fixture
def client():
    return ScraperServiceClient()


@pytest.mark.asyncio
async def test_scrape_success(client):
    with patch("app.scraper.service.scrape", new_callable=AsyncMock) as mock_scrape:
        mock_scrape.return_value = {
            "success": True,
            "content": "mocked article text",
            "url": "http://example.com",
            "engine_used": "playwright",
        }

        result = await client.scrape("http://example.com", engine="playwright")

        assert result["content"] == "mocked article text"
        mock_scrape.assert_called_once()
        args, kwargs = mock_scrape.call_args
        assert args[0] == "http://example.com"
        assert kwargs["engine"] == "playwright"


@pytest.mark.asyncio
async def test_scrape_failure(client):
    with patch("app.scraper.service.scrape", new_callable=AsyncMock) as mock_scrape:
        mock_scrape.return_value = {"success": False, "error": "timeout"}
        result = await client.scrape("http://example.com")
        assert result is None


@pytest.mark.asyncio
async def test_collect_success(client):
    with patch("app.scraper.service.collect", new_callable=AsyncMock) as mock_collect:
        mock_collect.return_value = {
            "source": "reddit",
            "count": 2,
            "items": [{"id": "1"}, {"id": "2"}],
        }

        result = await client.collect("reddit", {"subreddits": ["wallstreetbets"]})

        assert len(result) == 2
        assert result[0]["id"] == "1"
        mock_collect.assert_called_once()
        args, _ = mock_collect.call_args
        assert args[0] == "reddit"
        assert args[1] == {"subreddits": ["wallstreetbets"]}


@pytest.mark.asyncio
async def test_collect_error(client):
    with patch("app.scraper.service.collect", new_callable=AsyncMock) as mock_collect:
        mock_collect.side_effect = RuntimeError("boom")
        result = await client.collect("reddit", {"subreddits": ["wallstreetbets"]})
        assert result == []
