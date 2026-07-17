from abc import ABC, abstractmethod
from typing import Any

from app.scraper.core.base_result import ScrapeResult


class BaseEngine(ABC):
    """Abstract base class for all scraping engines.

    Each engine implements fetch() to retrieve and optionally
    extract data from a URL. Options dict is engine-specific:

        extract:    dict[str, str]  — CSS selector map {field: selector}
        wait_for:   str             — CSS selector to wait for (Playwright)
        scroll:     bool            — scroll to bottom first
        screenshot: bool            — capture screenshot
        prompt:     str             — VLM extraction prompt (vision engine)
        fast:       bool            — lightweight mode (crawl4ai)
    """

    @abstractmethod
    async def fetch(self, url: str, options: dict[str, Any]) -> ScrapeResult:
        ...

    async def health_check(self) -> bool:
        """Override in subclasses that need custom health probes."""
        return True
