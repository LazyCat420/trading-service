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
import datetime
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


def _rotator_env(monkeypatch, db):
    """Fake DB + seams so `_persist_articles` runs hermetically."""
    from app.collectors import news_api_rotator

    @contextmanager
    def fake_get_db():
        yield db

    monkeypatch.setattr(news_api_rotator, "get_db", fake_get_db)

    from app.collectors import news_collector as nc
    monkeypatch.setattr(nc, "url_fanout_exceeded", lambda db_, url, cap=None: False)

    class _FakeDedup:
        def __init__(self, *a, **k):
            pass

        def is_duplicate(self, *a):
            return False

        def compute_hash(self, *a):
            return "hash"

    import app.processors.dedup_engine as dedup_engine
    monkeypatch.setattr(dedup_engine, "DedupEngine", _FakeDedup)
    return news_api_rotator


def _rotator_article(tickers):
    from app.collectors.news_api_rotator import NewsArticle
    return NewsArticle(
        title="Chipmaker raises full-year guidance",
        url="https://example.com/b",
        summary=(
            "The company lifted its outlook after a strong quarter, citing "
            "accelerating datacenter demand and improved gross margin, and said "
            "it expects the trend to continue through the second half of the "
            "year as supply constraints ease and new capacity comes online."
        ),
        source="marketaux",
        published_at=datetime.datetime(2026, 8, 7, tzinfo=datetime.UTC),
        tickers=list(tickers),
    )


def test_rotator_marks_vendor_supplied_tickers_as_provider(monkeypatch):
    """Vendor entity tagging is NOT our text detection and must not wear the
    'detected' mark — the watch desk arms a trade-enabled wake on that label."""
    db = _RecorderDb()
    rotator = _rotator_env(monkeypatch, db)

    asyncio.run(rotator._persist_articles([_rotator_article(["NVDA"])]))

    inserts = _news_inserts(db)
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "ticker_attribution" in sql
    assert params[1] == "NVDA"
    assert params[-1] == "provider"


def test_rotator_marks_self_detected_tickers_as_detected(monkeypatch):
    """No vendor tickers -> we ran `_detect_tickers_in_text` ourselves."""
    db = _RecorderDb()
    rotator = _rotator_env(monkeypatch, db)

    from app.collectors import news_collector as nc

    async def finds_nvda(text):
        return {"NVDA"}

    monkeypatch.setattr(nc, "_detect_tickers_in_text", finds_nvda)

    asyncio.run(rotator._persist_articles([_rotator_article([])]))

    inserts = _news_inserts(db)
    assert len(inserts) == 1
    assert inserts[0][1][-1] == "detected"


def test_rotator_general_market_row_is_marked_not_null(monkeypatch):
    """ticker IS NULL, so there is no attribution to make — but the column must
    still be written, or NULL stops meaning 'legacy row'."""
    db = _RecorderDb()
    rotator = _rotator_env(monkeypatch, db)

    from app.collectors import news_collector as nc

    async def finds_nothing(text):
        return set()

    monkeypatch.setattr(nc, "_detect_tickers_in_text", finds_nothing)

    asyncio.run(rotator._persist_articles([_rotator_article([])]))

    inserts = _news_inserts(db)
    assert len(inserts) == 1
    _sql, params = inserts[0]
    assert params[1] is None          # general market news
    assert params[-1] == "general"    # ...still stamped


def test_every_news_insert_writes_ticker_attribution():
    """EVERY writer must stamp the column — not just the one with a unit test.

    The two tests above passed continuously while three of the five
    `INSERT INTO news_articles` paths never wrote `ticker_attribution` at all
    (`news_api_rotator.py` x2, the RSS writer in `news_collector.py`). They
    only ever exercised `collect_finnhub_news`, so they proved the concept on
    one writer and were blind to the other three. Measured 2026-08-07: 74.4%
    of rows collected in the previous 48 hours had NULL attribution, and
    `watch_desk._recent_news` admits NULL into a TRADE-ENABLED wake.

    This is a SCAN, not another per-writer case, so a sixth insert site added
    later fails here instead of silently reopening the hole.
    """
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2] / "app"
    pattern = re.compile(r"INSERT\s+INTO\s+news_articles\s*\(([^)]*)\)", re.I | re.S)

    offenders: list[str] = []
    found = 0
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            found += 1
            columns = {c.strip().lower() for c in match.group(1).split(",")}
            if "ticker_attribution" not in columns:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(app_dir.parent)}:{line}")

    assert found >= 5, (
        f"expected to find the known news_articles insert sites, saw {found} — "
        "the regex probably stopped matching, which would make this test "
        "vacuous rather than passing"
    )
    assert not offenders, (
        "these INSERT INTO news_articles sites do not write ticker_attribution, "
        "so their rows arrive NULL and are admitted by the watch desk's "
        f"trade-enabled wake filter: {offenders}"
    )


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
