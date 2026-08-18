import pytest
from unittest.mock import MagicMock, patch
from app.processors.dedup_engine import DedupEngine

@pytest.fixture
def mock_db():
    cursor = MagicMock()
    # Mock context manager yielding the cursor
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = cursor
    return cursor, db_ctx

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

def test_is_duplicate_tier1_hash(mock_db, monkeypatch):
    cursor, db_ctx = mock_db
    monkeypatch.setattr("app.processors.dedup_engine.get_db", lambda: db_ctx)
    
    # Mock finding a hash match in the database
    cursor.fetchone.return_value = ("some_id",)
    
    engine = DedupEngine(table="news_articles", ticker="AAPL")
    
    # Check duplicate
    assert engine.is_duplicate("Apple launches iPhone 16", "September event") is True
    
    # Verify execute was called with correct parameters
    execute_calls = cursor.execute.call_args_list
    assert len(execute_calls) == 1
    query, params = execute_calls[0][0]
    assert "content_hash" in query
    assert "ticker = %s" in query
    assert params[1] == "AAPL"

def test_is_duplicate_tier3_exact_title(mock_db, monkeypatch):
    cursor, db_ctx = mock_db
    monkeypatch.setattr("app.processors.dedup_engine.get_db", lambda: db_ctx)
    
    # Mock no hash match (return None)
    cursor.fetchone.return_value = None
    
    # Mock recent items fetched (return list of (title, summary) tuples)
    cursor.fetchall.return_value = [
        ("Breaking: Apple releases iOS 18!", "iOS 18 is out now.")
    ]
    
    engine = DedupEngine(table="news_articles", ticker="AAPL")
    
    # Check duplicate of the exact title (case-insensitive and prefix-stripped)
    assert engine.is_duplicate("Apple releases iOS 18!") is True

def test_is_duplicate_tier2_jaccard(mock_db, monkeypatch):
    cursor, db_ctx = mock_db
    monkeypatch.setattr("app.processors.dedup_engine.get_db", lambda: db_ctx)
    
    # Mock no hash match
    cursor.fetchone.return_value = None
    
    # Mock recent items in the DB
    cursor.fetchall.return_value = [
        ("Apple announces new iPhone 16 Pro Max at event", "The phone has an awesome new design and camera.")
    ]
    
    engine = DedupEngine(table="news_articles", ticker="AAPL", similarity_threshold=0.6)
    
    # Check duplicate with similar word overlap:
    # A lot of words overlap: "Apple announces iPhone 16 Pro Max"
    similar_text = "Apple announces iPhone 16 Pro Max with new features"
    assert engine.is_duplicate(similar_text) is True
    
    # Check non-duplicate with low overlap:
    different_text = "Microsoft launches new Windows 12 operating system"
    assert engine.is_duplicate(different_text) is False
