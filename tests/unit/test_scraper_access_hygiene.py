"""
Tests for scraper request hygiene and block detection.

The HTTP client sent only ``Accept: */*`` plus a User-Agent — no
Accept-Language, no Sec-Fetch-*, no Sec-Ch-Ua. No real browser produces that
combination, and four high-volume finance domains refused it outright. Adding
the headers Chrome actually sends flipped them (measured, back to back):

    marketwatch.com  401 -> 200      reuters.com    401 -> 200
    barrons.com      401 -> 200      thestreet.com  403 -> 200
"""
import pytest

from app.scraper.core.session_manager import DEFAULT_UA, browser_headers
from app.scraper.engines.auto_engine import AutoEngine


def test_headers_include_the_set_that_unblocked_real_domains():
    h = {k.lower(): v for k, v in browser_headers().items()}
    # Accept-Language alone recovered marketwatch and barrons.
    assert h["accept-language"].startswith("en-US")
    # A bare "*/*" Accept is the giveaway a browser never sends on navigation.
    assert h["accept"].startswith("text/html")
    for k in ("sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user"):
        assert k in h, f"missing {k}"
    assert h["sec-fetch-mode"] == "navigate"
    assert "sec-ch-ua" in h
    assert h["upgrade-insecure-requests"] == "1"


def test_user_agent_is_well_formed():
    """The vision engine's hand-written UA omitted the KHTML segment."""
    assert "(KHTML, like Gecko)" in DEFAULT_UA
    assert DEFAULT_UA.startswith("Mozilla/5.0 ")
    assert "Chrome/" in DEFAULT_UA and "Safari/" in DEFAULT_UA


def test_ua_is_consistent_between_client_and_headers():
    assert browser_headers()["User-Agent"] == DEFAULT_UA


def test_user_agent_override_is_respected(monkeypatch):
    monkeypatch.setenv("DEFAULT_USER_AGENT", "custom-agent/1.0")
    assert browser_headers()["User-Agent"] == "custom-agent/1.0"


@pytest.mark.parametrize("page", [
    # Verbatim from production. Each was previously returned as a successful
    # scrape and stored as the article body.
    "Access is temporarily restricted\n\nWe detected unusual activity from your device or network.",
    "Please let us know you're not a robot by clicking the box below.",
    "Checking your browser before accessing the site.",
    "Access Denied — you do not have permission to access this resource.",
    "Request blocked. We're sorry for the inconvenience.",
    "Rate limit exceeded, please try again later.",
])
def test_production_interstitials_are_detected(page):
    assert AutoEngine().is_blocked_content(page) is True


@pytest.mark.parametrize("page", [
    "Microsoft Corporation reported quarterly revenue of $62 billion, beating estimates.",
    "The Federal Reserve held rates steady on Wednesday, citing cooling inflation.",
    "Shares of Nvidia rose 3% after the company announced a new data centre partnership.",
])
def test_real_article_text_is_not_flagged(page):
    """The signature list must not swallow legitimate coverage of blocks/denials."""
    assert AutoEngine().is_blocked_content(page) is False


def test_empty_content_counts_as_blocked():
    assert AutoEngine().is_blocked_content("") is True
    assert AutoEngine().is_blocked_content(None) is True


def test_bloomberg_boilerplate_is_treated_as_a_block():
    """Bloomberg returns this same blurb for every URL when it refuses us.

    279 chars — past the length gate, and it trips no other signature, so it
    was being stored as the article body.
    """
    blurb = (
        "Connecting decision makers to a dynamic network of information, people "
        "and ideas, Bloomberg quickly and accurately delivers business and "
        "financial information, news and insight around the world"
    )
    assert len(blurb) > 150
    assert AutoEngine().is_blocked_content(blurb) is True
