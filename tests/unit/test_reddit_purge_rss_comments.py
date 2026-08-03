"""Reddit purge sweep must get COMMENTS via RSS when .json is bot-walled.

Regression for the starvation measured 2026-08-03: reddit's unauthenticated
.json endpoints 403 (WSL and NAS egress both), which silently emptied every
`get_thread_data` call — the mention counter ran on bare titles and the
trending feature produced 15 tickers lifetime. The thread's Atom feed still
serves (entry[0] = post, rest = comments; 429s are pacing, not blocking).
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.scraper.collectors.reddit_purge_collector import RedditPurgeCollector


ATOM_THREAD = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>What Are Your Moves Tomorrow, August 3, 2026</title>
  <entry>
    <title>What Are Your Moves Tomorrow, August 3, 2026</title>
    <content type="html">&lt;div&gt;Discuss your moves. $SPY calls anyone?&lt;/div&gt;</content>
  </entry>
  <entry>
    <title>/u/a on thread</title>
    <content type="html">&lt;p&gt;Loading up on TSLA and $NVDA before earnings&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>/u/b on thread</title>
    <content type="html">&lt;p&gt;[deleted]&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>/u/c on thread</title>
    <content type="html">&lt;p&gt;BABA is the play, not TSLA&lt;/p&gt;</content>
  </entry>
</feed>"""


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        raise ValueError("not json")


class _FakeClient:
    """Returns queued responses per URL-substring, recording every request."""

    def __init__(self, routes: list[tuple[str, _FakeResponse]]):
        self.routes = list(routes)
        self.requested: list[str] = []

    async def get(self, url, **kwargs):
        self.requested.append(url)
        for i, (frag, resp) in enumerate(self.routes):
            if frag in url:
                self.routes.pop(i)
                return resp
        return _FakeResponse(404)


@pytest.fixture
def collector(monkeypatch):
    col = RedditPurgeCollector()

    class _NoRate:
        def acquire(self, domain):
            class _Ctx:
                async def __aenter__(self):
                    return None
                async def __aexit__(self, *a):
                    return False
            return _Ctx()

    monkeypatch.setattr(
        "app.scraper.collectors.reddit_purge_collector.rate_limiter", _NoRate()
    )

    def install(routes):
        fake = _FakeClient(routes)
        monkeypatch.setattr(
            "app.scraper.collectors.reddit_purge_collector.session_manager",
            SimpleNamespace(client=fake),
        )
        return fake

    return col, install


def test_thread_comments_arrive_via_rss_first(collector):
    """RSS is tried FIRST — .json is 403-dead and only kept as a fallback."""
    col, install = collector
    fake = install([
        (".json", _FakeResponse(403)),
        (".rss", _FakeResponse(200, ATOM_THREAD)),
    ])
    title, selftext, comments = asyncio.run(
        col.get_thread_data("/r/wallstreetbets/comments/abc/moves/")
    )
    assert title.startswith("What Are Your Moves")
    assert "$SPY" in selftext
    # [deleted] dropped, real comments HTML-stripped
    assert comments == [
        "Loading up on TSLA and $NVDA before earnings",
        "BABA is the play, not TSLA",
    ]
    assert any(".rss?limit=100" in u for u in fake.requested)


def test_rss_429_retries_once_then_succeeds(collector, monkeypatch):
    col, install = collector
    naps = []

    async def fake_sleep(s):
        naps.append(s)

    monkeypatch.setattr(
        "app.scraper.collectors.reddit_purge_collector.asyncio.sleep", fake_sleep
    )
    install([
        (".json", _FakeResponse(403)),
        (".rss", _FakeResponse(429, headers={"retry-after": "3"})),
        (".rss", _FakeResponse(200, ATOM_THREAD)),
    ])
    title, _, comments = asyncio.run(col.get_thread_data("/r/stocks/comments/x/y/"))
    assert title and len(comments) == 2
    assert naps == [3.0]


def test_weighted_scoring_counts_comments(collector, monkeypatch):
    """Title x3, selftext x2, each comment x1 — the RedditPurgeScraper weights."""
    col, install = collector
    install([])

    async def fake_posts(sub, listing_type="hot", limit=5):
        if listing_type == "hot":
            return [{"id": "1", "title": "Daily Discussion", "subreddit": sub,
                     "permalink": f"/r/{sub}/comments/1/daily/", "score": 10,
                     "selftext": "", "num_comments": 5, "created_utc": 0,
                     "upvote_ratio": 0.9, "author": "mod"}]
        return []

    async def fake_thread(permalink):
        return ("TSLA earnings daily", "TSLA beat estimates", ["TSLA to the moon", "buy TSLA", "no, BABA"])

    monkeypatch.setattr(col, "get_subreddit_posts", fake_posts)
    monkeypatch.setattr(col, "get_thread_data", fake_thread)
    monkeypatch.setattr(col.validator, "validate_ticker", lambda t: t in {"TSLA", "BABA"})

    results = asyncio.run(col.collect(subreddits=["wallstreetbets"], limit=3))
    scores = {r["ticker"]: r["score"] for r in results}
    # TSLA: 3 (title) + 2 (body) + 2 comments = 7; BABA: 1 comment = 1
    assert scores == {"TSLA": 7, "BABA": 1}
    assert results[0]["ticker"] == "TSLA"
