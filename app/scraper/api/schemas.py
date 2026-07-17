"""
API Schemas — Request/Response models for scraper-service.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Scrape Endpoints ──

class ScrapeRequest(BaseModel):
    url: str
    engine: Literal["http", "playwright", "crawl4ai", "vision", "auto"] = "http"
    extract: dict[str, str] | None = None   # {field_name: css_selector}
    options: dict[str, Any] = Field(default_factory=dict)


class ScrapeResponse(BaseModel):
    url: str
    success: bool
    content: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    engine_used: str
    scraped_at: datetime
    status_code: int | None = None
    screenshot_b64: str | None = None


class BatchRequest(BaseModel):
    jobs: list[ScrapeRequest]
    max_concurrency: int = Field(default=5, ge=1, le=20)


# ── Collect Endpoints ──

class CollectRequest(BaseModel):
    source: Literal["reddit", "reddit-purge", "youtube", "news", "rss", "discourse", "xenforo", "kannapedia", "leafly", "duckduckgo", "twitter", "stocktwits", "finnews"]
    query: str | None = None
    subreddits: list[str] | None = None
    channels: list[str] | None = None
    feed_url: str | None = None
    feeds: dict[str, str] | None = None    # {feed_name: feed_url} for multi-feed
    keywords: list[str] | None = None
    usernames: list[str] | None = None     # Twitter users to scrape
    cashtags: list[str] | None = None      # Twitter $AAPL-style cashtags
    symbol: str | None = None              # StockTwits symbol
    # Financial news API fields
    tickers: list[str] | None = None        # Ticker symbols for API queries
    provider: str | None = None             # Specific provider (e.g. 'marketaux'), or None for all
    limit: int = Field(default=50, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)  # skip first N results (youtube search paging)
    from_date: str | None = None
    sort: str | None = None
    time_filter: str | None = None
    days_back: int | None = None
    require_transcript: bool = True
    stream: bool = False
    # Forum-specific fields
    base_url: str | None = None             # Forum base URL (e.g. https://overgrow.com)
    forum_name: str | None = None           # Display name for the forum
    subforum_path: str | None = None        # XenForo subforum path (e.g. f/grow-journals.54/)
    thread_url: str | None = None           # Specific thread URL to scrape posts from
    topic_id: int | None = None             # Discourse topic ID
    category_slug: str | None = None        # Discourse category slug
    category_id: int | None = None          # Discourse category ID
    tag: str | None = None                  # Discourse tag filter
    period: str | None = None               # Discourse top period (daily/weekly/monthly)
    rsp_numbers: list[str] | None = None     # Kannapedia RSP numbers to scrape
    # Reddit-purge specific fields
    use_llm: bool = False
    ollama_host: str | None = None
    ollama_model: str | None = None


class CollectResponse(BaseModel):
    source: str
    count: int
    items: list[dict[str, Any]]
    error: str | None = None
