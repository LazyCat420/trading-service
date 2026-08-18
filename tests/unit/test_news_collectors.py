"""News collector tests, ported off the inert `get_db` mock.

The old fixture patched `get_db` on `news_collector` and
`processors.dedup_engine`. `dedup_engine.get_db` no longer exists after the
Postgres->Mongo migration, and `news_collector` reads its publisher trust list
through `mongo_query.find_rows` and writes articles through
`mongo_store.upsert_doc` — so the mock intercepted almost nothing and both the
duplicate checks and the article writes went to the LIVE Mongo database. This
file patches `news_collector.mongo_query` / `news_collector.mongo_store` and
`dedup_engine.mongo_store`, and keeps a `get_db` mock only for the single
Postgres cursor `collect_finnhub_news` still opens. The
`"INSERT INTO news_articles" in sql` assertion became a structural assertion
on the `news_articles` collection and the titles in the written documents.
"""
import pytest
import datetime
from unittest.mock import patch, MagicMock

from app.collectors.news_api_rotator import NewsApiRotator, ProviderConfig, NewsArticle
from app.collectors.news_collector import collect_finnhub_news


class _Mongo:
    def __init__(self, store, query, dedup_store, db):
        self.store = store
        self.query = query
        self.dedup_store = dedup_store
        self.db = db

    def upserts(self, collection):
        """(key, doc) pairs upserted into `collection`."""
        return [
            (c[0][1], c[0][2])
            for c in self.store.upsert_doc.call_args_list
            if c[0][0] == collection
        ]


@pytest.fixture
def mongo():
    db = MagicMock()
    db.fetchone.return_value = None
    db.fetchall.return_value = []
    db.execute.return_value.fetchone.return_value = None
    db.execute.return_value.fetchall.return_value = []

    with patch("app.collectors.news_collector.get_db") as mock_get_db, \
         patch("app.collectors.news_collector.mongo_store") as store, \
         patch("app.collectors.news_collector.mongo_query") as query, \
         patch("app.processors.dedup_engine.mongo_store") as dedup_store:
        mock_get_db.return_value.__enter__.return_value = db

        # Configure defaults so DedupEngine checks don't see false positive
        # duplicates: nothing is stored yet.
        dedup_store.count_docs.return_value = 0
        dedup_store.find_docs.return_value = []
        # source_trust rows come back as TUPLES (source_name, win_rate,
        # total_items); an empty list means no publisher is banned.
        query.find_rows.return_value = []
        # agg_row backs url_fanout_exceeded; None => under the cap.
        query.agg_row.return_value = None
        store.writes_mongo.return_value = True
        store.writes_pg.return_value = False

        yield _Mongo(store, query, dedup_store, db)


@pytest.mark.asyncio
@patch("app.collectors.news_api_rotator._persist_articles")
async def test_news_api_rotator_429_fallback(mock_persist, mongo):
    """
    Test that if one provider fails (e.g. 429 Too Many Requests returning []),
    the rotator continues to the next provider and still successfully inserts data.
    """
    providers = [
        ProviderConfig("marketaux", "key1", daily_limit=10),
        ProviderConfig("newsapi", "key2", daily_limit=10)
    ]

    rotator = NewsApiRotator(providers=providers, tickers=["AAPL"])

    # We will mock _fetch_from_provider to return [] for the first and data for the second
    call_count = 0

    async def mock_fetch_from_provider(provider, query):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate 429 Too Many Requests -> Returns empty list
            return []
        else:
            # Second provider succeeds
            return [
                NewsArticle(
                    title="Apple announces new product",
                    url="https://example.com/apple",
                    summary="Apple is doing great.",
                    source=provider.name,
                    published_at=datetime.datetime.now(datetime.UTC),
                    tickers=["AAPL"]
                )
            ]

    mock_persist.return_value = 1

    with patch.object(rotator, "_fetch_from_provider", side_effect=mock_fetch_from_provider):
        async with rotator:
            count = await rotator.fetch_news(query="AAPL", persist=True)

    # We expect 1 article to be persisted
    assert count == 1
    mock_persist.assert_called_once()
    persisted_articles = mock_persist.call_args[0][0]
    assert len(persisted_articles) == 1
    assert persisted_articles[0].title == "Apple announces new product"


@pytest.mark.asyncio
@patch.dict("os.environ", {"FINNHUB_API_KEY": "fake_key"})
async def test_collect_finnhub_news_jaccard_dedup(mongo):
    """
    Test the Jaccard similarity threshold for identical articles in Finnhub.
    """
    # Summaries must be >= 150 chars to pass the STRICT QUALITY GATE
    long_summary_1 = "Apple reported record quarterly earnings, beating analyst expectations across all segments. " \
                      "Revenue came in above consensus estimates driven by strong iPhone and Services growth. " \
                      "The company also announced a massive stock buyback program."
    long_summary_2 = "Apple once again delivered a beat on quarterly earnings with revenue surpassing expectations. " \
                      "All segments contributed positively. The stock jumped in after-hours trading on " \
                      "the back of strong guidance for the next quarter ahead."
    long_summary_3 = "Tim Cook took the stage at WWDC to unveil a new set of iPhone features including advanced AI. " \
                      "The announcement was met with enthusiasm from developers and analysts alike. " \
                      "Apple Intelligence was the highlight of the keynote event."

    # Mock Finnhub client
    with patch("finnhub.Client") as mock_finnhub_class:
        mock_client = MagicMock()
        mock_finnhub_class.return_value = mock_client

        # We will return 3 articles:
        # 1. Base article
        # 2. Duplicate article (>60% similarity)
        # 3. Unique article (<60% similarity)
        mock_client.company_news.return_value = [
            {
                "headline": "Apple reports strong quarterly earnings and revenue beat",
                "summary": long_summary_1,
                "url": "http://example.com/1",
                "source": "Yahoo",
                "datetime": 1700000000
            },
            {
                # Duplicate: many shared words "Apple", "reports", "strong", "quarterly", "earnings", "revenue"
                "headline": "Apple reports strong quarterly earnings beating revenue expectations",
                "summary": long_summary_2,
                "url": "http://example.com/2",
                "source": "Finnhub",
                "datetime": 1700000001
            },
            {
                # Unique
                "headline": "Tim Cook announces new iPhone features at WWDC",
                "summary": long_summary_3,
                "url": "http://example.com/3",
                "source": "Bloomberg",
                "datetime": 1700000002
            }
        ]

        # Also mock the trust lookup to not drop these sources. find_rows
        # returns tuples (source_name, win_rate, total_items).
        mongo.query.find_rows.return_value = []

        count = await collect_finnhub_news("AAPL", days=7)

        # Expecting 2 articles to be inserted (the first and the third)
        assert count == 2
        # Verify exactly two documents landed in news_articles.
        writes = mongo.upserts("news_articles")
        assert len(writes) == 2

        # First call should be for WWDC (since it's newest by timestamp, it gets processed first due to sort!)
        # Wait, the sort puts newest first:
        # 1700000002 -> WWDC
        # 1700000001 -> Duplicate
        # 1700000000 -> Base
        # So WWDC is processed first, then Duplicate is processed. Duplicate and WWDC are not similar.
        # Then Base is processed. Base and Duplicate are similar! Since Duplicate was processed first, Base is skipped!
        # Either way, only 2 articles should make it through.
        inserted_titles = [doc["title"] for _key, doc in writes]
        assert any("WWDC" in title for title in inserted_titles)


def test_analyst_only_reference_filter():
    """
    Test that the analyst-only reference filter correctly distinguishes when a financial
    institution is mentioned as an analyst agency vs the subject of the news article.
    """
    from app.collectors.news_collector import _is_article_relevant_to_ticker

    # Case 1: Mentioned only as rating agency (should be filtered out for BAC)
    rating_article = (
        "Goldman Sachs analyst Catherine O'Brien raised the firm's price target on JetBlue "
        "Airways Corporation (JBLU) to $4.50 from $3.50. A day earlier, BofA analyst Andrew Didora "
        "increased his price target on JetBlue Airways Corporation (JBLU) to $4 from $3.50 while "
        "maintaining an Underperform rating."
    )
    assert not _is_article_relevant_to_ticker("BAC", rating_article)

    # Case 2: Bank stock itself is the subject (should NOT be filtered out)
    bank_news_article = (
        "Bank of America (BAC) shares climbed 2% in premarket trading after posting Q2 earnings "
        "that beat analyst consensus estimates on net interest income."
    )
    assert _is_article_relevant_to_ticker("BAC", bank_news_article)

    # Case 3: $BAC symbol is present (should NOT be filtered out)
    symbol_article = (
        "Active traders are tracking options activity for $BAC as large block trades suggest "
        "bullish positioning ahead of interest rate decisions."
    )
    assert _is_article_relevant_to_ticker("BAC", symbol_article)

    # Case 4: General article mentioning BofA Securities target, but no direct bank stock indicator
    general_rating = (
        "Apple target raised to $230 from $220 at BofA Securities on expectations of strong AI demand."
    )
    assert not _is_article_relevant_to_ticker("BAC", general_rating)
