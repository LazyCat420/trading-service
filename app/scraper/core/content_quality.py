"""
content_quality.py — is this actually an article, or just a header?
--------------------------------------------------------------------
A scrape can succeed, return HTTP 200, clear every block signature, and still
be worthless: many finance sites serve the headline plus one lede paragraph
and put the rest behind a paywall. Stored, that teaser is indistinguishable
from a full article — the failure is silent, which makes it worse than an
error.

Measured live through the deployed scraper, 6 URLs per domain (2026-08-09):

    domain                median chars   usable(>800)
    www.investors.com              526          0/6     never delivers
    seekingalpha.com                35          1/6     403 page x5
    www.bloomberg.com              585          1/6
    www.marketwatch.com            623          1/6
    www.cnbc.com                  1044          4/6
    www.tradingview.com           1472          3/6
    www.stocktitan.net             329          2/6     329 or 3000+, nothing between
    simplywall.st                 5277          6/6
    www.tradingkey.com            5645          6/6
    www.fool.com                 12035          6/6

Two things follow, and both shaped this module.

**Length is the signal, not a marker.** The thin bodies are not boilerplate to
pattern-match — Bloomberg's 745 chars and MarketWatch's 897 are genuine
truncated ledes, real prose that simply stops. What separates them from
articles is size, and the measured populations leave a clean gap:

    thin:  35 ... 726, 745, 811, 848, 897  |  full: 1044, 1241, 1275, 1472, ...

so the cut sits at 900.

**The domain is the wrong unit.** Four of the ten are bimodal — stocktitan
returns 329 or 3000+ with nothing between, tradingview 3/6, cnbc 4/6. A
denylist that removed those domains would throw away their real articles to
stop their teasers. So the gate judges the RESPONSE, and the skip list is
*earned* per domain from consecutive thin responses rather than hardcoded —
which also means a site that starts paywalling is caught without a code
change, and one that stops is picked back up on its own.
"""

import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Below this, a body is a headline + lede, not an article. From the measured
# gap between the two populations above.
MIN_ARTICLE_CHARS = int(os.getenv("SCRAPER_MIN_ARTICLE_CHARS", "900"))

# Consecutive thin responses, with no good one ever seen, before a domain
# stops being fetched at all. 5 is above the largest observed run of thin
# responses on a domain that also produces real articles (stocktitan's 4).
SKIP_AFTER_THIN = int(os.getenv("SCRAPER_SKIP_AFTER_THIN", "5"))

# How long a skipped domain stays skipped before one probe is allowed through.
# A site that fixes its paywall recovers by itself within a day.
SKIP_TTL_S = float(os.getenv("SCRAPER_SKIP_TTL_S", str(24 * 3600)))


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:  # noqa: BLE001 — a malformed URL has no domain
        return ""


def is_thin(text: str | None) -> bool:
    """True when the body is a header/teaser rather than an article."""
    return len(text or "") < MIN_ARTICLE_CHARS


def thin_reason(text: str | None) -> str:
    return (
        f"thin content ({len(text or '')} chars < {MIN_ARTICLE_CHARS}) — "
        "headline/teaser, not an article"
    )
