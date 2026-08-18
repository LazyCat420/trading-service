import pytest
from unittest.mock import MagicMock, patch
from app.processors.dedup_engine import DedupEngine

@pytest.fixture
def mongo():
    """Patch dedup_engine's Mongo store.

    The engine used to run two SQL statements -- a content_hash SELECT and a
    recent-items SELECT -- and the tests dispatched a cursor mock on
    fetchone/fetchall. It now calls `count_docs` for tier 1 and `find_docs`
    for tiers 2 and 3, so those are what the tests drive. `find_docs` returns
    DOCUMENTS, not (title, summary) tuples, and the engine reads them by
    column name.
    """
    store = MagicMock()
    store.count_docs.return_value = 0
    store.find_docs.return_value = []
    with patch("app.processors.dedup_engine.mongo_store", store):
        yield store

def test_normalize_text():
    engine = DedupEngine(table="news_articles")
    assert engine.normalize_text("Update: Apple releases iOS 18!") == "apple releases ios 18"
    assert engine.normalize_text("BREAKING: Market Crash!!!") == "market crash"
    assert engine.normalize_text("  some   spaces   ") == "some spaces"
    assert engine.normalize_text("") == ""

def test_get_word_set():
    engine = DedupEngine(table="news_articles")
    assert engine.get_word_set("Apple releases iOS 18!") == {"apple", "releases", "ios"}
    assert engine.get_word_set("the a to with") == set()  # too short (< 3 chars)

def test_compute_hash():
    engine = DedupEngine(table="news_articles")
    h1 = engine.compute_hash("Apple iOS 18", "Apple announces new iOS")
    h2 = engine.compute_hash("Apple iOS 18", "Apple announces new iOS")
    h3 = engine.compute_hash("Different Text", "entirely")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64

def test_is_duplicate_tier1_hash(mongo):
    # A content_hash match exists in the store
    mongo.count_docs.return_value = 1

    engine = DedupEngine(table="news_articles", ticker="AAPL")

    assert engine.is_duplicate("Apple launches iPhone 16", "September event") is True

    # Tier 1 short-circuits: the recent-items scan must not run.
    mongo.find_docs.assert_not_called()
    collection, query = mongo.count_docs.call_args[0][:2]
    assert collection == "news_articles"
    assert "content_hash" in query
    # The ticker must be part of the filter, and upper-cased — a hash match on
    # a DIFFERENT ticker is not a duplicate for this one.
    assert query["ticker"] == "AAPL"


def test_tier1_hash_match_on_another_ticker_is_not_a_duplicate(mongo):
    """The ticker has to reach the filter, not just the log line."""
    mongo.count_docs.return_value = 0
    engine = DedupEngine(table="news_articles", ticker="AAPL")
    assert engine.is_duplicate("Apple launches iPhone 16", "September event") is False

def test_is_duplicate_tier3_exact_title(mongo):
    # No hash match, so tiers 2/3 run against the recent documents
    mongo.count_docs.return_value = 0
    mongo.find_docs.return_value = [
        {"title": "Breaking: Apple releases iOS 18!", "summary": "iOS 18 is out now."}
    ]
    
    engine = DedupEngine(table="news_articles", ticker="AAPL")
    
    # Check duplicate of the exact title (case-insensitive and prefix-stripped)
    assert engine.is_duplicate("Apple releases iOS 18!") is True

def test_is_duplicate_tier2_jaccard(mongo):
    mongo.count_docs.return_value = 0
    mongo.find_docs.return_value = [
        {"title": "Apple announces new iPhone 16 Pro Max at event",
         "summary": "The phone has an awesome new design and camera."}
    ]
    
    engine = DedupEngine(table="news_articles", ticker="AAPL", similarity_threshold=0.6)
    
    # Check duplicate with similar word overlap:
    # A lot of words overlap: "Apple announces iPhone 16 Pro Max"
    similar_text = "Apple announces iPhone 16 Pro Max with new features"
    assert engine.is_duplicate(similar_text) is True
    
    # Check non-duplicate with low overlap:
    different_text = "Microsoft launches new Windows 12 operating system"
    assert engine.is_duplicate(different_text) is False
