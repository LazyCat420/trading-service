"""url_fanout_exceeded — the per-URL insert cap for news_articles."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.collectors import news_collector
from app.collectors.news_collector import url_fanout_exceeded


@contextmanager
def _count(n):
    """Stub the existing-rows count for the URL.

    `agg_row('news_articles', {'url': url}, [('count', None)])` returns a
    TUPLE, so the cap compares row[0]. Patching the module's `mongo_query`
    also asserts the filter is keyed on the url and nothing else — the old
    fake DB only recorded SQL text, so a count over the wrong filter would
    have passed.
    """
    query = MagicMock()
    query.agg_row.return_value = (n,)
    with patch.object(news_collector, "mongo_query", query):
        yield query


class _BoomQuery:
    def agg_row(self, *a, **k):
        raise RuntimeError("db down")


def test_under_cap_allows_insert():
    with _count(4):
        assert url_fanout_exceeded(None, "http://x", cap=5) is False


def test_at_cap_blocks_insert():
    with _count(5) as query:
        assert url_fanout_exceeded(None, "http://x", cap=5) is True
    collection, filt, _agg = query.agg_row.call_args[0][:3]
    assert collection == "news_articles"
    assert filt == {"url": "http://x"}


def test_no_url_allows():
    with _count(99) as query:
        assert url_fanout_exceeded(None, None, cap=5) is False
        assert url_fanout_exceeded(None, "", cap=5) is False
        query.agg_row.assert_not_called()


def test_cap_zero_disables():
    with _count(99) as query:
        assert url_fanout_exceeded(None, "http://x", cap=0) is False
        query.agg_row.assert_not_called()


def test_fails_open_on_db_error():
    with patch.object(news_collector, "mongo_query", _BoomQuery()):
        assert url_fanout_exceeded(None, "http://x", cap=5) is False


def test_default_cap_from_settings():
    with _count(1_000):
        assert url_fanout_exceeded(None, "http://x") is True
    with _count(0):
        assert url_fanout_exceeded(None, "http://x") is False
