"""An unknown must not be reported as a measurement.

Every case here is the same defect wearing different clothes: a field the
transport could not supply was written as 0, "" or now(), which is a CLAIM. A
blank invites a question; a 0 closes it. Each one had a real consumer treating
the fabricated value as data.
"""
import time
from datetime import datetime, timedelta
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from app.scraper.collectors.news_collector import NewsCollector
from app.scraper.engines.auto_engine import AutoEngine


# ── A dateless RSS entry ─────────────────────────────────────────────────────

def test_a_feed_entry_with_no_date_reports_none_not_now():
    """utcnow() laundered a dateless entry past the consumer's
    `published_at_estimated` flag — the flag that exists for exactly this."""
    got = NewsCollector()._parse_feed_date(SimpleNamespace())
    assert got is None, "an unknown publication date must arrive as null"


def test_a_feed_entry_with_a_date_still_parses():
    """The positive control, and it must use a real struct_time.

    A plain tuple has no `.tm_year`, so feeding one exercises the parser's
    `except` and returns None — which looks like the code is broken when it is
    the harness that is. feedparser hands over a struct_time; so does this.
    """
    entry = SimpleNamespace(
        published_parsed=time.struct_time((2026, 9, 1, 12, 30, 0, 0, 244, 0))
    )
    got = NewsCollector()._parse_feed_date(entry)
    assert got == datetime(2026, 9, 1, 12, 30, 0)


def test_a_dateless_entry_is_not_mistaken_for_a_fresh_one():
    """The consequence, stated as the consumer sees it: a fabricated now() is
    inside every freshness window there is."""
    got = NewsCollector()._parse_feed_date(SimpleNamespace())
    cutoff = datetime.utcnow() - timedelta(hours=1)
    assert not (got is not None and got > cutoff), \
        "an undated article must not pass a 1-hour freshness filter"


# ── The bot-wall classifier ──────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "Before we continue... Press & Hold to confirm you are a human (and not a bot).",
    "Checking your browser before accessing the site. cloudflare",
    "Access denied. You do not have permission to access this page.",
    "You're loading pages faster than a human can. Automatic cooldown in effect.",
])
def test_a_real_interstitial_is_still_caught(body):
    """The signatures must keep working — this is the direction that protects
    the corpus from storing a bot-wall as an article body."""
    assert AutoEngine().is_blocked_content(body) is True


@pytest.mark.parametrize("subject", [
    "Cloudflare", "access denied", "rate limit exceeded", "too many requests",
])
def test_a_long_article_mentioning_a_signature_is_not_a_bot_wall(subject):
    """Cloudflare, Inc. trades as NET and is covered by exactly the finance
    sites this scraper reads. Matched as a bare substring across 15,000 chars,
    any article ABOUT it was classified as a bot-wall, escalated to Playwright,
    and returned as `auto (failed)` — a silent, ticker-shaped coverage hole."""
    article = (
        f"Shares rose after the company addressed {subject} in its earnings call. "
        "The chief executive said demand held up through the quarter. "
    ) * 60          # ~7,000 chars: unmistakably an article
    assert len(article) > 2000
    assert AutoEngine().is_blocked_content(article) is False


def test_an_empty_body_is_still_blocked():
    assert AutoEngine().is_blocked_content("") is True


def test_the_length_cut_sits_between_the_measured_populations():
    """Derived, not pinned to a magic number: the measured walls in auto_engine
    are 279 and 464 chars, and content_quality puts the thin/article boundary at
    900. The cut must sit above the former and clear of the latter."""
    from app.scraper.engines.auto_engine import _MAX_BLOCK_PAGE_CHARS
    from app.scraper.core import content_quality
    assert _MAX_BLOCK_PAGE_CHARS > 464, "must still see the largest measured wall"
    assert _MAX_BLOCK_PAGE_CHARS > content_quality.MIN_ARTICLE_CHARS


# ── The DuckDuckGo gate ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_ddg_gate_lives_at_the_shared_surface(monkeypatch):
    """DISABLE_DDG_SEARCH had ONE reader, so the /collect route and youtube's
    search path ignored it and still paid an HTTP attempt plus a full Chromium
    launch for a page DDG will not serve this IP."""
    from app.scraper.collectors.duckduckgo_collector import DuckDuckGoCollector
    monkeypatch.setenv("DISABLE_DDG_SEARCH", "true")

    called = []
    collector = DuckDuckGoCollector()
    monkeypatch.setattr(collector, "_search_http",
                        lambda *a, **k: called.append("http"))
    monkeypatch.setattr(collector, "_search_playwright",
                        lambda *a, **k: called.append("playwright"))

    assert await collector.search("anything") == []
    assert called == [], "a disabled search must cost nothing, not just return nothing"


@pytest.mark.asyncio
async def test_the_gate_is_read_at_call_time_not_import_time(monkeypatch):
    """It is appended to the remote .env at deploy time, so a value captured at
    import would be the wrong one."""
    from app.scraper.collectors import duckduckgo_collector as ddg
    monkeypatch.setenv("DISABLE_DDG_SEARCH", "false")
    assert ddg.ddg_disabled() is False
    monkeypatch.setenv("DISABLE_DDG_SEARCH", "true")
    assert ddg.ddg_disabled() is True


# ── Provider sentiment ───────────────────────────────────────────────────────

def test_an_unscored_alphavantage_article_is_not_neutral():
    """0.0 on AlphaVantage's scale MEANS neutral, so `or 0` turned "the provider
    did not score this" into a measured verdict — and marketaux, which gets it
    right, became incomparable."""
    import inspect
    from app.scraper.collectors import finnews_collector
    src = inspect.getsource(finnews_collector)
    assert 'float(item.get("overall_sentiment_score", 0) or 0)' not in src
    assert "_raw_sentiment" in src


def test_finnews_encodes_the_query_it_interpolates():
    """"Procter & Gamble guidance" sent q="Procter " plus a spurious parameter,
    and the provider answered 200 for the truncated query — silently wrong
    rather than an error."""
    import inspect
    from app.scraper.collectors import finnews_collector
    src = inspect.getsource(finnews_collector)
    for raw in ("?q={query}", "keywords={query}", "search={query}", "text={query}"):
        assert raw not in src, f"unencoded interpolation still present: {raw}"
    assert "quote_plus(query)" in src


# ── The engine must match the caller's budget ────────────────────────────────

@pytest.mark.asyncio
async def test_the_news_sweep_does_not_ask_for_an_engine_it_cannot_wait_for(monkeypatch):
    """A 4s cap cannot receive an `auto` result.

    Server-side, `auto` is a sequential ladder whose HTTP leg alone carries a
    30s httpx timeout before it escalates to Playwright. So this side always
    cancelled first — and FastAPI does not cancel a non-streaming handler on
    client disconnect, so the server then ran the whole ladder and launched a
    browser for a body that had already been discarded. Every escalation the cap
    paid for was guaranteed waste, on exactly the JS-rendered pages that needed
    it most.
    """
    from app.collectors import news_collector as nc

    seen = {}

    async def _fake(url, max_chars=15000, engine="auto", timeout_s=None):
        seen["engine"] = engine
        seen["timeout_s"] = timeout_s
        return "body " * 100

    monkeypatch.setattr(nc, "_scrape_article_body_via_service", _fake)
    await nc._scrape_with_timeout("http://x.test/a", "FALLBACK", timeout=4.0)

    assert seen["engine"] == "http", "a 4s budget must not request the escalating engine"
    assert seen["timeout_s"] == 4.0, "the server must be told the deadline too"


@pytest.mark.asyncio
async def test_the_body_upgrade_pass_still_gets_the_escalating_engine(monkeypatch):
    """The other direction: this pass has a ~20s budget, which CAN receive a
    Playwright escalation. Dropping `auto` everywhere would have quietly removed
    the service's whole reason for having an auto engine.

    Patched on `news_collector`, not on `body_upgrade`: the helper is imported
    lazily inside the function, so a module attribute on body_upgrade is not the
    seam the code actually crosses.
    """
    from datetime import UTC
    from app.collectors import body_upgrade as bu
    from app.collectors import news_api_rotator as nar
    from app.collectors.news_api_rotator import NewsArticle

    seen = {}

    async def _fake(url, max_chars=15000, engine="auto", timeout_s=None):
        seen["engine"] = engine
        seen["timeout_s"] = timeout_s
        return "w" * 3000

    art = NewsArticle(title="A headline", url="http://x.test/a", summary="b" * 300,
                      source="polygon", published_at=datetime(2026, 8, 9, tzinfo=UTC))
    with patch("app.collectors.news_collector._scrape_article_body_via_service", _fake):
        await nar._upgrade_bodies([art])

    assert seen.get("engine") == "auto", "the 20s pass must keep the escalating engine"
    assert seen.get("timeout_s") == bu.UPGRADE_BUDGET_S
