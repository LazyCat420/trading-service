from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ScrapeResult:
    """Standard result object returned by all scraping engines.

    Every engine (http, playwright, crawl4ai, vision) normalizes
    its output into this shape so callers never need to know
    which engine was used.
    """

    url: str
    success: bool
    content: str | None        # raw HTML or extracted text
    data: dict[str, Any]       # extracted fields (CSS selector map results)
    error: str | None
    engine_used: str
    scraped_at: datetime
    status_code: int | None = None
    screenshot_b64: str | None = None  # Playwright/vision only
    links: list[dict] = field(default_factory=list)
    media: list[dict] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
