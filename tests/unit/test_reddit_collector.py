"""tests/unit/test_reddit_collector.py
---------------------------------------
Unit test suite for RedditCollector (RSS-First + YARS features: post details & user data).
"""

import asyncio
from types import SimpleNamespace
import pytest

from app.scraper.collectors.reddit_collector import RedditCollector


ATOM_THREAD = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>What Are Your Moves Tomorrow</title>
  <entry>
    <title>What Are Your Moves Tomorrow</title>
    <summary>&lt;div&gt;Discuss your moves. $SPY calls anyone?&lt;/div&gt;</summary>
    <author><name>/u/mod</name></author>
  </entry>
  <entry>
    <title>/u/trader1 on thread</title>
    <summary>&lt;p&gt;Loading up on TSLA before earnings&lt;/p&gt;</summary>
    <author><name>/u/trader1</name></author>
  </entry>
  <entry>
    <title>/u/trader2 on thread</title>
    <summary>&lt;p&gt;[deleted]&lt;/p&gt;</summary>
    <author><name>/u/trader2</name></author>
  </entry>
</feed>"""

ATOM_USER_SUBMISSIONS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Submissions by lazy_trader</title>
  <entry>
    <title>My DD on NVDA</title>
    <link href="https://www.reddit.com/r/stocks/comments/nvda123/my_dd/"/>
    <updated>2026-08-01T10:00:00Z</updated>
    <summary>&lt;p&gt;Here is why NVDA will grow.&lt;/p&gt;</summary>
  </entry>
</feed>"""

ATOM_USER_COMMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Comments by lazy_trader</title>
  <entry>
    <title>lazy_trader on My DD on NVDA</title>
    <link href="https://www.reddit.com/r/stocks/comments/nvda123/comment/c1/"/>
    <updated>2026-08-01T11:00:00Z</updated>
    <summary>&lt;p&gt;Agreed, revenue is solid.&lt;/p&gt;</summary>
  </entry>
</feed>"""


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


class _FakeClient:
    def __init__(self, routes: list[tuple[str, _FakeResponse]]):
        self.routes = list(routes)

    async def get(self, url, **kwargs):
        for frag, resp in self.routes:
            if frag in url:
                return resp
        return _FakeResponse(404)


@pytest.fixture
def collector(monkeypatch):
    col = RedditCollector()

    class _NoRate:
        def acquire(self, domain):
            class _Ctx:
                async def __aenter__(self):
                    return None
                async def __aexit__(self, *a):
                    return False
            return _Ctx()

    monkeypatch.setattr(
        "app.scraper.collectors.reddit_collector.rate_limiter", _NoRate()
    )

    def install(routes):
        fake = _FakeClient(routes)
        monkeypatch.setattr(
            "app.scraper.collectors.reddit_collector.session_manager",
            SimpleNamespace(client=fake),
        )
        return fake

    return col, install


@pytest.mark.asyncio
async def test_scrape_post_details(collector):
    col, install = collector
    install([
        (".rss", _FakeResponse(200, ATOM_THREAD)),
    ])
    res = await col.scrape_post_details("/r/wallstreetbets/comments/abc/moves/")
    assert res["title"] == "What Are Your Moves Tomorrow"
    assert "$SPY" in res["selftext"]
    assert res["num_comments"] == 1
    assert res["comments"][0]["author"] == "/u/trader1"
    assert "TSLA" in res["comments"][0]["body"]


@pytest.mark.asyncio
async def test_scrape_user_data(collector):
    col, install = collector
    install([
        ("submitted.rss", _FakeResponse(200, ATOM_USER_SUBMISSIONS)),
        ("comments.rss", _FakeResponse(200, ATOM_USER_COMMENTS)),
    ])
    res = await col.scrape_user_data("lazy_trader", limit=5)
    assert res["username"] == "lazy_trader"
    assert res["submissions_count"] == 1
    assert res["comments_count"] == 1
    assert res["submissions"][0]["title"] == "My DD on NVDA"
    assert res["comments"][0]["body"] == "Agreed, revenue is solid."
