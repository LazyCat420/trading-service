"""url_fanout_exceeded — the per-URL insert cap for news_articles."""

from app.collectors.news_collector import url_fanout_exceeded


class _FakeDB:
    def __init__(self, count):
        self._count = count
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        return self

    def fetchone(self):
        return (self._count,)


class _BoomDB:
    def execute(self, *a, **k):
        raise RuntimeError("db down")


def test_under_cap_allows_insert():
    assert url_fanout_exceeded(_FakeDB(4), "http://x", cap=5) is False


def test_at_cap_blocks_insert():
    assert url_fanout_exceeded(_FakeDB(5), "http://x", cap=5) is True


def test_no_url_allows():
    db = _FakeDB(99)
    assert url_fanout_exceeded(db, None, cap=5) is False
    assert url_fanout_exceeded(db, "", cap=5) is False
    assert db.queries == []


def test_cap_zero_disables():
    db = _FakeDB(99)
    assert url_fanout_exceeded(db, "http://x", cap=0) is False
    assert db.queries == []


def test_fails_open_on_db_error():
    assert url_fanout_exceeded(_BoomDB(), "http://x", cap=5) is False


def test_default_cap_from_settings():
    assert url_fanout_exceeded(_FakeDB(1_000), "http://x") is True
    assert url_fanout_exceeded(_FakeDB(0), "http://x") is False
