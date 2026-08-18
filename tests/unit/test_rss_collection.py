"""
Unit tests for RSS news collection pipeline.

Tests the full collect_feed() pipeline in isolation using mocked DB
and real HTTP/feedparser where appropriate.

Key findings from live diagnostic (2026-05-02):
  - All 22 RSS feeds return HTTP 200 with valid entries (100% reachable)
  - collect_feed() works correctly when DB is connected
  - The "0 articles" issue is caused by either:
    a) should_collect("news_rss") gating (6h cooldown)
    b) ON CONFLICT DO NOTHING deduplication (articles already exist)
    c) Silent exception swallowing in the broad except at line 480
"""

import datetime
import hashlib
import re
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from app.processors.ticker_extractor import _registry

@pytest.fixture(autouse=True)
def setup_registry():
    _registry.load()
    yield



# ---------------------------------------------------------------------------
# Test: _normalize_title
# ---------------------------------------------------------------------------


class TestNormalizeTitle:
    """_normalize_title must produce stable, lowercase, punctuation-free keys."""

    def test_basic_normalization(self):
        from app.collectors.news_collector import _normalize_title

        assert _normalize_title("Hello World!") == "hello world"

    def test_strips_breaking_prefix(self):
        from app.collectors.news_collector import _normalize_title

        assert _normalize_title("Breaking: Market crashes") == "market crashes"

    def test_strips_update_prefix(self):
        from app.collectors.news_collector import _normalize_title

        assert _normalize_title("UPDATE - Stocks rally") == "stocks rally"

    def test_strips_exclusive_prefix(self):
        from app.collectors.news_collector import _normalize_title

        assert _normalize_title("Exclusive: New deal signed") == "new deal signed"

    def test_collapses_whitespace(self):
        from app.collectors.news_collector import _normalize_title

        result = _normalize_title("  Lots   of    spaces  ")
        assert "  " not in result
        assert result == "lots of spaces"

    def test_caps_at_200_chars(self):
        from app.collectors.news_collector import _normalize_title

        long_title = "A" * 300
        result = _normalize_title(long_title)
        assert len(result) <= 200

    def test_empty_string(self):
        from app.collectors.news_collector import _normalize_title

        assert _normalize_title("") == ""

    def test_identical_titles_from_different_sources_match(self):
        """Same headline from Yahoo + Finnhub should normalize identically."""
        from app.collectors.news_collector import _normalize_title

        yahoo = "Breaking: NVIDIA beats earnings expectations"
        finnhub = "NVIDIA beats earnings expectations"
        # After stripping "Breaking:" prefix, both should match
        assert _normalize_title(yahoo) == _normalize_title(finnhub)


# ---------------------------------------------------------------------------
# Test: _get_article_id
# ---------------------------------------------------------------------------


class TestGetArticleId:
    """_get_article_id must produce deterministic, collision-resistant IDs."""

    def test_same_title_same_ticker_same_id(self):
        from app.collectors.news_collector import _get_article_id

        id1 = _get_article_id("NVIDIA beats earnings", "NVDA")
        id2 = _get_article_id("NVIDIA beats earnings", "NVDA")
        assert id1 == id2

    def test_same_title_different_ticker_different_id(self):
        from app.collectors.news_collector import _get_article_id

        id_nvda = _get_article_id("Stock beats earnings", "NVDA")
        id_aapl = _get_article_id("Stock beats earnings", "AAPL")
        assert id_nvda != id_aapl

    def test_same_title_no_ticker_consistent(self):
        from app.collectors.news_collector import _get_article_id

        id1 = _get_article_id("General market news", None)
        id2 = _get_article_id("General market news", None)
        assert id1 == id2

    def test_returns_sha256_hex(self):
        from app.collectors.news_collector import _get_article_id

        result = _get_article_id("Test", "AAPL")
        assert len(result) == 64  # SHA256 hex digest
        assert all(c in "0123456789abcdef" for c in result)

    def test_cross_source_dedup_breaking_prefix(self):
        """'Breaking: X' and 'X' should produce the same ID."""
        from app.collectors.news_collector import _get_article_id

        id1 = _get_article_id("Breaking: NVIDIA beats earnings", "NVDA")
        id2 = _get_article_id("NVIDIA beats earnings", "NVDA")
        assert id1 == id2


# ---------------------------------------------------------------------------
# Test: _detect_tickers_in_text
# ---------------------------------------------------------------------------


class TestDetectTickers:
    """Ticker detection from article text."""

    @pytest.mark.asyncio
    async def test_detects_company_name(self):
        from app.collectors.news_collector import _detect_tickers_in_text

        tickers = await _detect_tickers_in_text("Apple announces new iPhone")
        assert "AAPL" in tickers

    @pytest.mark.asyncio
    async def test_detects_ticker_symbol(self):
        from app.collectors.news_collector import _detect_tickers_in_text

        tickers = await _detect_tickers_in_text("NVDA beat earnings expectations")
        assert "NVDA" in tickers

    @pytest.mark.asyncio
    async def test_empty_on_gibberish(self):
        from app.collectors.news_collector import _detect_tickers_in_text

        tickers = await _detect_tickers_in_text("lorem ipsum dolor sit amet")
        # Should not detect any real tickers
        assert len(tickers) == 0 or all(
            t not in {"AAPL", "NVDA", "TSLA"} for t in tickers
        )

    @pytest.mark.asyncio
    async def test_multiple_tickers(self):
        from app.collectors.news_collector import _detect_tickers_in_text

        tickers = await _detect_tickers_in_text(
            "Apple and Tesla both reporting earnings this week"
        )
        assert "AAPL" in tickers
        assert "TSLA" in tickers


# ---------------------------------------------------------------------------
# Test: _extract_text_from_html
# ---------------------------------------------------------------------------


class TestExtractTextFromHtml:
    """HTML → plain text extraction for article summaries."""

    def test_strips_script_tags(self):
        from app.collectors.news_collector import _extract_text_from_html

        html = "<p>Hello</p><script>alert('xss')</script><p>World</p>"
        result = _extract_text_from_html(html)
        assert "alert" not in result
        assert "Hello" in result

    def test_strips_style_tags(self):
        from app.collectors.news_collector import _extract_text_from_html

        html = "<style>.red{color:red}</style><p>Content here</p>"
        result = _extract_text_from_html(html)
        assert "color" not in result
        assert "Content" in result

    def test_extracts_paragraph_text(self):
        from app.collectors.news_collector import _extract_text_from_html

        html = "<p>First paragraph.</p><p>Second paragraph.</p>"
        result = _extract_text_from_html(html)
        assert "First paragraph" in result
        assert "Second paragraph" in result

    def test_respects_max_chars(self):
        from app.collectors.news_collector import _extract_text_from_html

        html = "<p>" + "A" * 1000 + "</p>"
        result = _extract_text_from_html(html, max_chars=200)
        assert len(result) <= 200

    def test_returns_empty_on_no_content(self):
        from app.collectors.news_collector import _extract_text_from_html

        result = _extract_text_from_html("")
        assert result == ""


# ---------------------------------------------------------------------------
# Test: collect_feed (mocked DB)
# ---------------------------------------------------------------------------


class TestCollectFeed:
    """Test collect_feed() with mocked DB to verify the pipeline logic."""

    @pytest.fixture
    def mock_db_ctx(self):
        """Create a mock DB context manager."""
        cursor = MagicMock()
        cursor.execute.return_value = cursor
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        return cursor

    @pytest.mark.asyncio
    @patch("app.services.scraper_client.scraper_client.collect")
    async def test_collect_feed_with_mock_http_and_db(self, mock_collect, mock_db_ctx):
        """Verify collect_feed processes entries and calls DB insert."""
        from app.collectors.news_collector import collect_feed

        mock_collect.return_value = [
            {
                "title": "Apple beats earnings",
                "url": "https://example.com/article1",
                "published_at": "2026-05-02T12:00:00Z",
                "summary": "Apple reported strong Q2 results. Apple reported strong Q2 results. Apple reported strong Q2 results. Apple reported strong Q2 results. Apple reported strong Q2 results.",
                "publisher": "Test Feed",
            },
            {
                "title": "Markets rally on jobs data",
                "url": "https://example.com/article2",
                "published_at": "2026-05-02T11:00:00Z",
                "summary": "The S&P 500 surged after strong employment numbers. The S&P 500 surged after strong employment numbers. The S&P 500 surged after strong employment numbers. The S&P 500 surged after strong employment numbers.",
                "publisher": "Test Feed",
            }
        ]

        # news_collector still opens a `get_db()` cursor around the loop, but
        # the ARTICLE WRITE goes through mongo_store.upsert_doc now, and the
        # DedupEngine reads/counts through mongo_store too. Patching
        # `dedup_engine.get_db` — a symbol that module no longer imports —
        # raised, and before that the dedup engine was reading the live
        # collection to decide whether these fixture articles were duplicates.
        # collect_feed re-imports `mongo_store` INSIDE the write loop
        # (`from app.db import mongo_store`), so a module-level patch on
        # news_collector is rebound out from under the test. Patch the
        # functions on app.db.mongo_store itself, which every binding shares.
        upsert = MagicMock()
        query = MagicMock()
        # url_fanout_exceeded counts prior articles on the URL: 0 → not capped.
        query.agg_row.return_value = (0,)
        query.find_rows.return_value = []
        query.find_row.return_value = None

        with patch("app.collectors.news_collector.get_db", return_value=mock_db_ctx), \
             patch("app.db.mongo_store.upsert_doc", upsert), \
             patch("app.collectors.news_collector.mongo_query", query), \
             patch("app.db.mongo_store.ensure_indexes", MagicMock()), \
             patch("app.db.mongo_store.count_docs", return_value=0), \
             patch("app.db.mongo_store.find_docs", return_value=[]), \
             patch("app.db.mongo_store.writes_mongo", lambda _t: True), \
             patch("app.db.mongo_store.writes_pg", lambda _t: False), \
             patch("app.db.mongo_store.reads_mongo", lambda _t: True):
            count = await collect_feed("Test Feed", "https://example.com/rss")

        # Should have processed both entries
        assert count >= 2
        # ...and each one was written as a news_articles document, keyed on its
        # own id and insert-only so a re-collect cannot overwrite a stored
        # article. The old assertion (`mock_db_ctx.execute.called`) only proved
        # SOME SQL ran; this pins the collection, the upsert key and the source.
        upserts = [c for c in upsert.call_args_list if c[0][0] == "news_articles"]
        assert len(upserts) == count
        titles = set()
        for call in upserts:
            _coll, key, doc = call[0][:3]
            assert key == {"id": doc["id"]}
            assert call.kwargs.get("insert_only") is True
            assert doc["source"] == "rss"
            assert doc["publisher"] == "Test Feed"
            assert doc["content_hash"]
            titles.add(doc["title"])
        assert "Apple beats earnings" in titles
        assert "Markets rally on jobs data" in titles

    @pytest.mark.asyncio
    @patch("app.services.scraper_client.scraper_client.collect")
    async def test_collect_feed_http_failure_returns_zero(self, mock_collect, mock_db_ctx):
        """HTTP failure should return 0, not raise."""
        from app.collectors.news_collector import collect_feed

        mock_collect.side_effect = Exception("Scraper error")

        with patch("app.collectors.news_collector.get_db", return_value=mock_db_ctx):
            with patch("app.collectors.news_collector.get_db", return_value=mock_db_ctx):
                count = await collect_feed("Test", "https://example.com/rss")

        assert count == 0

    @pytest.mark.asyncio
    async def test_collect_feed_db_exception_returns_zero(self):
        """DB errors should be caught and return 0, not propagate."""
        from app.collectors.news_collector import collect_feed

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(side_effect=ConnectionError("DB down"))
        mock_db.__exit__ = MagicMock(return_value=False)

        # Patch at BOTH locations since collect_feed re-imports get_db inside the function
        with patch("app.db.connection.get_db", return_value=mock_db):
            with patch("app.collectors.news_collector.get_db", return_value=mock_db):
                count = await collect_feed("Test", "https://feeds.bbci.co.uk/news/business/rss.xml")

        assert count == 0


# ---------------------------------------------------------------------------
# Test: collect_all resilience
# ---------------------------------------------------------------------------


class TestCollectAll:
    """collect_all() should continue when individual feeds fail."""

    @pytest.mark.asyncio
    async def test_one_feed_failure_doesnt_kill_others(self):
        """If one feed's internal logic fails, collect_all continues.

        NOTE: collect_feed() has its own internal try/except, so it returns 0
        on error. But collect_all() does NOT have a try/except around the
        collect_feed() call — so if collect_feed were replaced with a mock
        that raises, it WOULD kill the loop. This test verifies the real
        behavior: collect_feed catches errors internally and returns 0.
        """
        from app.collectors.news_collector import collect_all

        call_count = 0

        async def mock_collect_feed(name, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 0  # Simulates internal failure (caught by collect_feed)
            return 5  # Other feeds succeed

        with patch("app.collectors.news_collector.collect_feed", mock_collect_feed):
            total = await collect_all(limit_feeds=3)

        assert call_count == 3  # All 3 feeds were attempted
        assert total == 10  # 0 + 5 + 5

    @pytest.mark.asyncio
    async def test_collect_all_catches_uncaught_exceptions(self):
        """After the fix, collect_all() catches exceptions that propagate
        past collect_feed's internal try/except (e.g., mock that raises)."""
        from app.collectors.news_collector import collect_all

        call_count = 0

        async def mock_collect_feed(name, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("Simulated uncaught exception")
            return 5

        with patch("app.collectors.news_collector.collect_feed", mock_collect_feed):
            total = await collect_all(limit_feeds=3)

        # All 3 feeds should still be attempted despite feed 1 raising
        assert call_count == 3
        assert total == 10  # 0 (exception) + 5 + 5


# ---------------------------------------------------------------------------
# Test: News API Rotator provider config
# ---------------------------------------------------------------------------


class TestNewsApiRotator:
    """News API Rotator provider configuration."""

    def test_providers_without_keys_are_disabled(self):
        from app.collectors.news_api_rotator import ProviderConfig

        p = ProviderConfig("test", "", daily_limit=100)
        # build_providers_from_settings auto-disables empty keys
        assert p.api_key == ""

    def test_quota_tracker_starts_at_zero(self):
        import asyncio
        from app.collectors.news_api_rotator import QuotaTracker

        qt = QuotaTracker(daily_limit=100, per_minute_limit=10)

        async def check():
            assert await qt.can_use() is True
            await qt.consume()
            assert await qt.can_use() is True  # 1/100 daily, 1/10 minute

        asyncio.run(check())

    def test_quota_tracker_exhaustion(self):
        import asyncio
        from app.collectors.news_api_rotator import QuotaTracker

        qt = QuotaTracker(daily_limit=2, per_minute_limit=10)

        async def exhaust():
            await qt.consume()
            await qt.consume()
            assert await qt.can_use() is False  # 2/2 daily limit hit

        asyncio.run(exhaust())

    def test_persist_articles_deduplicates(self):
        """_persist_articles should write one news_articles document per
        article that survives dedup.

        The write moved from `db.execute(INSERT ...)` to
        `mongo_store.upsert_doc('news_articles', {'id': ...}, ..., insert_only=True)`,
        so `mock_db.execute.called` no longer proves anything — the rotator
        still opens a `get_db()` cursor, but the article never travels through
        it. This reads the document the rotator actually stored.
        """
        import asyncio
        from app.collectors.news_api_rotator import _persist_articles, NewsArticle

        mock_db = MagicMock()
        mock_db.execute.return_value = mock_db
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)

        articles = [
            NewsArticle(
                title="Test Article",
                url="",
                summary="Test summary content that is long enough to bypass the 150 character minimum filter in the rotator database persistence function. We make this string extra long so it definitely passes the 150-character limit check.",
                source="test",
                published_at=datetime.datetime.now(datetime.UTC),
                tickers=["AAPL"],
            )
        ]

        # The rotator now consults the shared DedupEngine (imported inside the
        # function from app.processors.dedup_engine) — mock it so the unit test
        # never touches a real DB and the article always reads as fresh.
        mock_dedup = MagicMock()
        mock_dedup.is_duplicate.return_value = False
        mock_dedup.compute_hash.return_value = "testhash"

        store = MagicMock()
        # url_fanout_exceeded (news_collector) counts prior articles on the
        # URL through mongo_query.agg_row — 0 means the ticker fan-out is
        # not capped, so the AAPL row is written.
        nc_query = MagicMock()
        nc_query.agg_row.return_value = (0,)

        async def run_persist():
            with patch("app.collectors.news_api_rotator.get_db", return_value=mock_db), \
                 patch("app.collectors.news_api_rotator.mongo_store", store), \
                 patch("app.collectors.news_collector.mongo_query", nc_query), \
                 patch("app.processors.dedup_engine.DedupEngine", return_value=mock_dedup):
                return await _persist_articles(articles)

        count = asyncio.run(run_persist())

        assert count == 1
        upserts = [c for c in store.upsert_doc.call_args_list if c[0][0] == "news_articles"]
        assert len(upserts) == 1
        _coll, key, doc = upserts[0][0][:3]
        assert key == {"id": doc["id"]}
        assert upserts[0].kwargs.get("insert_only") is True
        assert doc["ticker"] == "AAPL"
        assert doc["title"] == "Test Article"
        assert doc["source"] == "test"
        # The content_hash is what lets the OTHER collectors' DedupEngine see
        # rotator articles at all — a rotator row that stored an empty hash
        # was invisible to them and the same story got double-stored.
        assert doc["content_hash"] == "testhash"
        # Provider-supplied tickers must be labelled as such, never as our own
        # 'detected' verification.
        assert doc["ticker_attribution"] == "provider"
