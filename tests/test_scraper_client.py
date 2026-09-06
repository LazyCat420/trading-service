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
        # The payload comes back even on failure: callers gate on `success`,
        # and `error`/`engine_used` are the only way to tell a skipped domain
        # from a bot-wall from a dead URL.
        assert result == {"success": False, "error": "timeout"}
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


# ── The ledger: a server-side collector crash is an outage, not an empty result ──

@pytest.mark.asyncio
async def test_a_200_with_an_error_field_counts_as_a_failure(client):
    """collect.py answers 200 + `error` ONLY from its blanket except — i.e. a
    collector raised. That is the shape of the 13-day vision outage and of a
    partial-copy ImportError. It used to log a warning and touch nothing, so
    `failures` stayed 0 through a total outage and the sweep was stamped ✅."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _FakeResponse({
            "source": "reddit", "count": 0, "items": [],
            "error": "No module named 'app.utils.async_utils'",
        })
        result = await client.collect("reddit", {"subreddits": ["x"]})

    assert result == []
    assert client.failures == 1, "a collector crash must reach the ledger"
    assert "async_utils" in (client.last_error or "")
    assert "reddit" in (client.last_error or ""), "the source must be named"


@pytest.mark.asyncio
async def test_a_genuinely_empty_source_is_not_a_failure(client):
    """The other direction — without this, the check above could be satisfied
    by counting every quiet weekend as an outage."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _FakeResponse({"source": "reddit", "count": 0, "items": []})
        result = await client.collect("reddit", {"subreddits": ["x"]})

    assert result == []
    assert client.failures == 0
    assert client.calls == 1


@pytest.mark.asyncio
async def test_a_sweep_record_ignores_a_concurrent_callers_failures(client):
    """reset_failures() + read cannot separate two sweeps sharing the singleton.

    Concretely: the discovery sweep zeroes the counter, then the nightly
    StockTwits pass errors three times, and discovery reports "3 calls errored"
    for work it never did. A delta cannot make that mistake.
    """
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("someone else's outage")
        await client.collect("stocktwits", {})       # a concurrent caller

        rec = client.sweep()                          # our sweep starts here
        await client.collect("news", {})              # our one failure

    assert client.failures == 2, "the shared ledger still sees both"
    assert rec.failures == 1, "but the sweep only owns its own"
    assert rec.calls == 1
    assert rec.failure_rate == 1.0
    assert "news" in (rec.last_error or "")


@pytest.mark.asyncio
async def test_a_sweep_that_did_nothing_wrong_reports_a_clean_rate(client):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _FakeResponse({"source": "news", "count": 1, "items": [{}]})
        rec = client.sweep()
        await client.collect("news", {})

    assert rec.failed is False
    assert rec.failure_rate == 0.0
    assert rec.last_error is None


@pytest.mark.asyncio
async def test_failure_rate_is_a_rate_not_a_count(client):
    """A 1-in-40 blip and a 40-in-40 outage must not read the same. The old
    test was `failures and not total_scraped`, which fires on both."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        rec = client.sweep()
        mock_post.return_value = _FakeResponse({"source": "news", "count": 1, "items": [{}]})
        for _ in range(39):
            await client.collect("news", {})
        mock_post.side_effect = httpx.ConnectError("one blip")
        await client.collect("news", {})

    assert rec.calls == 40
    assert rec.failures == 1
    assert rec.failure_rate == pytest.approx(0.025)
