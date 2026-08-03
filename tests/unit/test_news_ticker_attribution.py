"""Fallback-stamped news rows must be marked, and must not wake cycles.

Regression for the ghost-wake measured 2026-08-02/03: when ticker extraction
finds nothing in a Finnhub per-ticker article, the row inherits the QUERIED
ticker. One generic "Earnings, PMI and Other Key Things to Watch this Week"
roundup was stored 5x under LLY/SHOP/MCD/UBER/PFE (quality_status='ok') and
its keyword match woke the LLY trade-enabled cycle. Rows are kept (Finnhub
asserts per-company relevance) but stamped ticker_attribution='query_fallback',
and the Watch Desk's news read refuses to trip on them.
"""

import asyncio
import sys
import types
from contextlib import contextmanager

import pytest

from app.collectors import news_collector


class _RecorderDb:
    def __init__(self):
        self.executed: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params or []))

        class _R:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None

        return _R()


@pytest.fixture
def finnhub_env(monkeypatch):
    """Fake finnhub client + DB + seams so collect_finnhub_news runs hermetically."""
    db = _RecorderDb()

    @contextmanager
    def fake_get_db():
        yield db

    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.setattr(news_collector, "get_db", fake_get_db)
    monkeypatch.setattr(news_collector, "url_fanout_exceeded", lambda db_, url, cap=None: False)

    article = {
        "headline": "Earnings, PMI and Other Key Things to Watch this Week",
        "summary": (
            "A generic market preview mentioning nothing in particular. "
            "Investors will digest a heavy slate of quarterly results, a fresh "
            "purchasing managers index reading, and remarks from central bank "
            "officials, with futures pointing modestly higher ahead of the open "
            "and volatility gauges drifting near their lowest levels of the "
            "summer as positioning stays cautious into the data."
        ),
        "source": "Yahoo",
        "url": "https://example.com/a",
        "datetime": 1785700000,
    }

    fake_finnhub = types.ModuleType("finnhub")

    class _Client:
        def __init__(self, api_key):
            pass

        def company_news(self, ticker, _from=None, to=None):
            return [dict(article)]

    fake_finnhub.Client = _Client
    monkeypatch.setitem(sys.modules, "finnhub", fake_finnhub)

    class _FakeDedup:
        def __init__(self, *a, **k):
            pass

        def is_duplicate(self, *a):
            return False

        def compute_hash(self, *a):
            return "hash"

    import app.processors.dedup_engine as dedup_engine
    monkeypatch.setattr(dedup_engine, "DedupEngine", _FakeDedup)
    return db


def _news_inserts(db):
    return [
        (sql, params) for sql, params in db.executed
        if "INSERT INTO news_articles" in sql
    ]


def test_unmatched_article_is_stamped_query_fallback(finnhub_env, monkeypatch):
    async def no_tickers(text):
        return set()

    monkeypatch.setattr(news_collector, "_detect_tickers_in_text", no_tickers)
    asyncio.run(news_collector.collect_finnhub_news("LLY"))

    inserts = _news_inserts(finnhub_env)
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "ticker_attribution" in sql
    assert params[1] == "LLY"           # inherited the queried ticker...
    assert params[-1] == "query_fallback"  # ...but wears the mark


def test_detected_article_is_stamped_detected(finnhub_env, monkeypatch):
    async def finds_lly(text):
        return {"LLY"}

    monkeypatch.setattr(news_collector, "_detect_tickers_in_text", finds_lly)
    monkeypatch.setattr(news_collector, "_is_article_relevant_to_ticker", lambda t, txt: True)
    asyncio.run(news_collector.collect_finnhub_news("LLY"))

    inserts = _news_inserts(finnhub_env)
    assert len(inserts) == 1
    assert inserts[0][1][-1] == "detected"


def test_watch_desk_news_read_refuses_fallback_rows(monkeypatch):
    """The wake trigger's SQL must exclude query_fallback and discarded rows
    while keeping legacy NULLs eligible."""
    from app.services import watch_desk

    db = _RecorderDb()

    @contextmanager
    def fake_get_db():
        yield db

    monkeypatch.setattr(watch_desk, "get_db", fake_get_db)
    watch_desk._recent_news("LLY")

    sql = db.executed[0][0]
    assert "ticker_attribution IS NULL OR ticker_attribution != 'query_fallback'" in sql
    assert "quality_status IS NULL OR quality_status != 'discarded'" in sql
