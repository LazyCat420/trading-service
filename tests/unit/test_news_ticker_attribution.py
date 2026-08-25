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
from unittest.mock import MagicMock

import pytest

from app.collectors import news_collector


class _RecorderDb:
    def __init__(self):
        self.executed: list[tuple[str, list]] = []
        #: (collection, key, document) for every Mongo upsert the writer issued
        self.mongo_writes: list[tuple[str, dict, dict]] = []

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
    """Fake finnhub client + seams so collect_finnhub_news runs hermetically.

    `news_collector` is off Postgres entirely, so there is no `get_db` left to
    patch; `_RecorderDb` survives only as the recorder the Mongo write seam
    below appends its documents to.
    """
    db = _RecorderDb()

    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.setattr(news_collector, "url_fanout_exceeded", lambda db_, url, cap=None: False)

    # The finnhub writer upserts into Mongo now. Record the documents so the
    # attribution assertions can read the field instead of a SQL param index.
    store = MagicMock()
    store.upsert_doc.side_effect = (
        lambda coll, key, doc, **kw: db.mongo_writes.append((coll, key, doc))
    )
    store.writes_mongo.return_value = True
    store.writes_pg.return_value = False
    monkeypatch.setattr(news_collector, "mongo_store", store)

    # The reads have to be stubbed too. `collect_finnhub_news` pulls
    # `source_trust` through mongo_query before it writes anything, and that
    # read is inside the function's broad `except`, so an unstubbed one is
    # swallowed as "[news] Finnhub LLY error" and the writer is simply never
    # reached — the assertion then fails on an empty list with no clue why.
    query = MagicMock()
    query.find_rows.return_value = []
    query.agg_row.return_value = (0,)
    monkeypatch.setattr(news_collector, "mongo_query", query)

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

    writes = [w for w in finnhub_env.mongo_writes if w[0] == "news_articles"]
    assert len(writes) == 1
    doc = writes[0][2]
    assert doc["ticker"] == "LLY"                      # inherited the queried ticker...
    assert doc["ticker_attribution"] == "query_fallback"  # ...but wears the mark


def test_detected_article_is_stamped_detected(finnhub_env, monkeypatch):
    async def finds_lly(text):
        return {"LLY"}

    monkeypatch.setattr(news_collector, "_detect_tickers_in_text", finds_lly)
    monkeypatch.setattr(news_collector, "_is_article_relevant_to_ticker", lambda t, txt: True)
    asyncio.run(news_collector.collect_finnhub_news("LLY"))

    writes = [w for w in finnhub_env.mongo_writes if w[0] == "news_articles"]
    assert len(writes) == 1
    assert writes[0][2]["ticker_attribution"] == "detected"


def _rotator_env(monkeypatch, db):
    """Fake store + seams so `_persist_articles` runs hermetically.

    The rotator writes through `mongo_store.upsert_doc` now and no longer
    imports `get_db` at all. `db` is still passed so `_rotator_writes` can read
    what was recorded, but the recorder captures Mongo documents rather than
    SQL and params.
    """
    from app.collectors import news_api_rotator

    store = MagicMock()
    store.upsert_doc.side_effect = lambda coll, key, doc, **kw: db.mongo_writes.append((coll, key, doc))
    monkeypatch.setattr(news_api_rotator, "mongo_store", store)

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

    writes = [w for w in db.mongo_writes if w[0] == "news_articles"]
    assert len(writes) == 1
    _coll, _key, doc = writes[0]
    assert doc["ticker"] == "NVDA"
    assert doc["ticker_attribution"] == "provider"


def test_rotator_marks_self_detected_tickers_as_detected(monkeypatch):
    """No vendor tickers -> we ran `_detect_tickers_in_text` ourselves."""
    db = _RecorderDb()
    rotator = _rotator_env(monkeypatch, db)

    from app.collectors import news_collector as nc

    async def finds_nvda(text):
        return {"NVDA"}

    monkeypatch.setattr(nc, "_detect_tickers_in_text", finds_nvda)

    asyncio.run(rotator._persist_articles([_rotator_article([])]))

    writes = [w for w in db.mongo_writes if w[0] == "news_articles"]
    assert len(writes) == 1
    assert writes[0][2]["ticker_attribution"] == "detected"


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

    writes = [w for w in db.mongo_writes if w[0] == "news_articles"]
    assert len(writes) == 1
    doc = writes[0][2]
    assert doc["ticker"] is None            # general market news
    assert doc["ticker_attribution"] == "general"   # ...still stamped


def test_every_news_write_stamps_ticker_attribution():
    """EVERY writer must stamp the field — not just the one with a unit test.

    The per-writer tests above passed continuously while three of the five
    news_articles write paths never wrote `ticker_attribution` at all
    (`news_api_rotator.py` x2, the RSS writer in `news_collector.py`). They
    only ever exercised `collect_finnhub_news`, so they proved the concept on
    one writer and were blind to the other three. Measured 2026-08-07: 74.4%
    of rows collected in the previous 48 hours had NULL attribution, and
    `watch_desk._recent_news` admits NULL into a TRADE-ENABLED wake.

    This is a SCAN, not another per-writer case, so a sixth write site added
    later fails here instead of silently reopening the hole.

    It reads the AST rather than the source text. The Postgres version matched
    `INSERT INTO news_articles (...)` with a regex and counted the columns; the
    Mongo writers pass a dict literal to `upsert_doc`/`insert_docs`, so the
    regex found nothing and the scan would have reported success over zero
    sites — which is why it carries its own floor.
    """
    import ast
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2] / "app"

    def _writes_news_articles(node: ast.Call) -> ast.Dict | None:
        """The document dict for a news_articles write, if this call is one."""
        fn = getattr(node.func, "attr", None)
        if fn not in ("upsert_doc", "insert_docs", "bulk_upsert"):
            return None
        if not node.args:
            return None
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "news_articles"):
            return None
        for arg in node.args[1:]:
            if isinstance(arg, ast.Dict) and len(arg.keys) > 3:
                return arg          # upsert_doc(coll, key, DOC)
            if isinstance(arg, ast.List) and arg.elts:
                inner = arg.elts[0]
                if isinstance(inner, ast.Dict):
                    return inner    # insert_docs(coll, [DOC])
        return None

    offenders: list[str] = []
    found = 0
    for path in sorted(app_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            doc = _writes_news_articles(node)
            if doc is None:
                continue
            found += 1
            fields = {k.value for k in doc.keys
                      if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "ticker_attribution" not in fields:
                offenders.append(f"{path.relative_to(app_dir.parent)}:{node.lineno}")

    assert found >= 5, (
        f"expected to find the known news_articles write sites, saw {found} — "
        "the matcher probably stopped matching, which would make this test "
        "vacuous rather than passing"
    )
    assert not offenders, (
        "these news_articles writes do not stamp ticker_attribution, "
        "so their rows arrive without it and are admitted by the watch desk's "
        f"trade-enabled wake filter: {offenders}"
    )


def test_watch_desk_news_read_refuses_fallback_rows(monkeypatch):
    """The wake trigger must exclude query_fallback and discarded rows while
    keeping legacy NULLs eligible.

    Asserted against the FILTER's behaviour, not its text: the Mongo query is
    applied to representative documents, so a filter that stopped excluding
    (or started over-excluding NULLs) fails here regardless of how it is
    spelled.
    """
    from app.services import watch_desk

    captured = {}

    def fake_find_rows(collection, query, columns, sort=None, limit=0):
        captured["collection"] = collection
        captured["query"] = query
        return []

    fake_q = types.SimpleNamespace(find_rows=fake_find_rows)
    monkeypatch.setattr(watch_desk, "mongo_query", fake_q)
    watch_desk._recent_news("LLY")

    assert captured["collection"] == "news_articles"
    q = captured["query"]

    def matches(doc):
        """Does this document satisfy the filter's attribution/quality parts?"""
        for field in ("ticker_attribution", "quality_status"):
            cond = q[field]
            # Both operators are null-tolerant, which is the property this
            # guard exists to pin: $ne and $nin each match a MISSING or null
            # field, i.e. the exact translation of `(col IS NULL OR col != x)`.
            # $nin arrived 2026-08-25 when `provider_unverified` joined
            # `query_fallback` on the refused list.
            assert set(cond) <= {"$ne", "$nin"}, f"unexpected operator on {field}: {cond}"
            refused = [cond["$ne"]] if "$ne" in cond else list(cond["$nin"])
            if doc.get(field) is not None and doc.get(field) in refused:
                return False
        return True

    # Refused.
    assert not matches({"ticker_attribution": "query_fallback"})
    assert not matches({"ticker_attribution": "provider_unverified"})
    assert not matches({"quality_status": "discarded"})
    # Legacy NULLs and absent fields stay eligible — tightening these to
    # fail-closed would blind every watch on pre-2026-08-07 rows.
    assert matches({})
    assert matches({"ticker_attribution": None, "quality_status": None})
    # A vendor-tagged, detected, or thin-quality row still trips a wake.
    assert matches({"ticker_attribution": "detected", "quality_status": "thin"})
    assert matches({"ticker_attribution": "provider"})
