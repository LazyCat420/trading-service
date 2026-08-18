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


# ---------------------------------------------------------------------------
# RSS carries no engagement data. These pin that we record its absence instead
# of substituting a plausible number, and that the quality gate still filters
# on something real once the number is gone.
# ---------------------------------------------------------------------------

ATOM_SUBREDDIT = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t3_aaa111</id>
    <title>DD: why margins expand next quarter</title>
    <link href="https://www.reddit.com/r/stocks/comments/aaa111/dd/"/>
    <updated>2026-08-10T10:00:00Z</updated>
    <author><name>/u/analyst</name></author>
    <summary>&lt;p&gt;Datacenter revenue guidance was raised while opex stayed flat,
      which is the whole argument for multiple expansion here. Buybacks continue
      and the balance sheet carries no near-term maturities worth worrying about.&lt;/p&gt;</summary>
  </entry>
  <entry>
    <id>t3_bbb222</id>
    <title>Daily Discussion Thread</title>
    <link href="https://www.reddit.com/r/stocks/comments/bbb222/daily/"/>
    <updated>2026-08-10T09:00:00Z</updated>
    <author><name>/u/automod</name></author>
    <summary>&lt;p&gt;[link]&lt;/p&gt;</summary>
  </entry>
  <entry>
    <id>t3_ccc333</id>
    <title>Deleted thoughts</title>
    <link href="https://www.reddit.com/r/stocks/comments/ccc333/x/"/>
    <updated>2026-08-10T08:00:00Z</updated>
    <author><name>/u/gone</name></author>
    <summary>&lt;p&gt;[removed]&lt;/p&gt;</summary>
  </entry>
</feed>"""


@pytest.mark.asyncio
async def test_rss_records_absent_engagement_as_none(collector):
    """RSS publishes no score/comments/ratio, so we must store None.

    A default like 100 is indistinguishable from a measured 100 once written,
    and every downstream filter and sort then ranks against a constant.
    """
    col, install = collector
    install([(".rss", _FakeResponse(200, ATOM_SUBREDDIT))])

    posts = await col._fetch_subreddit_rss("stocks", "hot", "day", 10)

    assert posts, "RSS fixture should parse to posts"
    for p in posts:
        assert p["score"] is None
        assert p["num_comments"] is None
        assert p["upvote_ratio"] is None
        assert p["_source"] == "rss"
    # The fields RSS DOES carry must survive.
    assert posts[0]["title"] == "DD: why margins expand next quarter"
    assert "Datacenter revenue" in posts[0]["selftext"]


def test_quality_gate_filters_on_content_when_score_is_unknown():
    """With no engagement signal, the gate must still reject junk.

    Before this change the gate thresholded a hardcoded score=100/comments=10
    and therefore passed every RSS post -- a check that passed for both states.
    """
    from app.scraper.collectors.reddit_collector import _is_quality_post

    substantive = {"title": "DD on margins", "selftext": "y" * 200,
                   "score": None, "num_comments": None}
    megathread = {"title": "Daily Discussion", "selftext": "",
                  "score": None, "num_comments": None}
    removed = {"title": "gone", "selftext": "[removed]",
               "score": None, "num_comments": None}
    untitled = {"title": "", "selftext": "y" * 200,
                "score": None, "num_comments": None}

    assert _is_quality_post(substantive) is True
    assert _is_quality_post(megathread) is False
    assert _is_quality_post(removed) is False
    assert _is_quality_post(untitled) is False


def test_quality_gate_still_honours_a_real_score():
    """The .json path can still supply real numbers; it must stay strict."""
    from app.scraper.collectors.reddit_collector import _is_quality_post

    low_engagement = {"title": "meh", "selftext": "y" * 200,
                      "score": 1, "num_comments": 0}
    assert _is_quality_post(low_engagement) is False

    strong = {"title": "real", "selftext": "y" * 200,
              "score": 500, "num_comments": 120}
    assert _is_quality_post(strong) is True


def test_store_post_survives_an_unknown_comment_count():
    """comment_velocity divided by a None count, inside a bare except.

    That combination silently dropped every RSS post while logging at INFO,
    so the table would empty out and the collector would look healthy.
    """
    from unittest.mock import MagicMock, patch

    from app.collectors import reddit_collector as rc

    captured = []

    store = MagicMock()
    store.upsert_doc.side_effect = lambda collection, key, doc, **_kw: captured.append(
        (collection, key, doc)
    )

    post = {"id": "abc", "title": "DD", "body": "y" * 200,
            "score": None, "num_comments": None, "upvote_ratio": None,
            "created_at": "2026-08-10T10:00:00+00:00"}

    with patch.object(rc, "mongo_store", store):
        assert rc._store_post(post, "NVDA", "stocks", set()) == 1

    collection, key, doc = captured[0]
    assert collection == "reddit_posts"
    assert key == {"id": "abc_NVDA"}
    # score / upvote_ratio / comment_count / comment_velocity all NULL, not 0.
    assert doc["score"] is None
    assert doc["upvote_ratio"] is None
    assert doc["comment_count"] is None
    assert doc["comment_velocity"] is None
