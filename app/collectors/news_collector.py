"""
News Collector -- Fetches financial news from RSS feeds + web sources.

Pure data collector. No LLM calls. No processing.
Writes to: news_articles
No API key needed -- uses scraper-service.
Dedup: hash(title + published_at) as id.
"""

import logging
import hashlib
import re
import datetime
import asyncio
import time
from app.db.connection import get_db
from app.processors.ticker_extractor import get_ticker_symbols
from app.utils.text_utils import is_truncated_content, is_scrape_artifact


def quality_at_write(title: str, summary: str) -> tuple[str, str]:
    """Write-time quality gate for news_articles.

    Historically NO insert path set quality_status (95% of rows were NULL, and
    bot-wall/captcha pages were stored as articles — the detector only ran at
    evidence-READ time). Every insert now stamps a verdict:
      discarded — scrape artifact (captcha/block page/near-empty)
      thin      — no summary beyond the title (kept, but flagged)
      ok        — passed the write gate
    Vocabulary matches pending_review.py ('ok'/'rejected') + the historical
    'discarded' used by the purge flow, so readers can filter on one set.
    """
    if is_scrape_artifact(summary or "") or is_scrape_artifact(title or ""):
        return "discarded", "scrape_artifact_at_write"
    if not (summary or "").strip():
        return "thin", "empty_summary_at_write"
    return "ok", "write_gate"

logger = logging.getLogger(__name__)

# Truncated/paywalled drops are routine (dozens per collection pass) — the old
# per-article WARNING was the single loudest line in the container log. Titles
# now log at DEBUG; this counter surfaces an aggregate INFO line periodically.
_drop_counts: dict[str, int] = {}


def _note_drop(source: str, title: str, provider: str, length: int) -> None:
    logger.debug("[news][DROP] %s: dropped '%s' from %s — truncated/paywalled (len=%d)",
                 source, title[:60], provider, length)
    _drop_counts[source] = _drop_counts.get(source, 0) + 1
    n = _drop_counts[source]
    if n % 50 == 0:
        logger.info("[news] %s: %d truncated/paywalled articles dropped since boot", source, n)

# RSS feeds to monitor
RSS_FEEDS = {
    # ── Market News (tier 1 — highest volume) ──
    "MarketWatch Top": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "MarketWatch Markets": "https://feeds.marketwatch.com/marketwatch/marketpulse/",
    "CNBC Top": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "CNBC Finance": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
    "CNBC Markets": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "Bloomberg Markets": "https://feeds.bloomberg.com/markets/news.rss",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "Google News Business": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB",
    # ── Analysis / Research ──
    "Seeking Alpha": "https://seekingalpha.com/market_currents.xml",
    "Benzinga": "https://www.benzinga.com/feed",
    "Business Insider": "https://www.businessinsider.com/rss",
    "Kiplinger": "https://www.kiplinger.com/feed/all",
    "Investing.com": "https://www.investing.com/rss/news.rss",
    "Nasdaq News": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    # ── Wire services / broadsheet ──
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "NPR Business": "https://feeds.npr.org/1006/rss.xml",
    "The Guardian Business": "https://www.theguardian.com/uk/business/rss",
    "FT Markets": "https://www.ft.com/rss/home/uk",
    # ── Government / macro ──
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "US Treasury": "https://home.treasury.gov/rss.xml",
    # ── Crypto ──
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    # ── Newsletters / Macro Blogs ──
    "Calculated Risk": "https://www.calculatedriskblog.com/feeds/posts/default",
    "Marginal Revolution": "https://marginalrevolution.com/feed",
    "Visual Capitalist": "https://www.visualcapitalist.com/feed/",
    "ZeroHedge": "https://feeds.feedburner.com/zerohedge/feed",
}

# Foreign Language RSS Feeds (Requires Translation)
FOREIGN_RSS_FEEDS = {
    # ── Asian Markets (Japanese & Chinese) ──
    "Nikkei JP": "https://assets.nikkei.jp/data/rss/news/market.rdf",
    "Sina Finance CN": "https://rss.sina.com.cn/roll/finance/hot_roll.xml",
    # ── European Markets (German & French) ──
    "Handelsblatt DE": "https://www.handelsblatt.com/contentexport/feed/finanzen",
    "Les Echos FR": "https://services.lesechos.fr/rss/les-echos-finance-marches.xml",
}

# Company name -> ticker mapping
COMPANY_TICKERS = {
    # Tech mega-caps
    "nvidia": "NVDA",
    "apple": "AAPL",
    "tesla": "TSLA",
    "microsoft": "MSFT",
    "google": "GOOG",
    "alphabet": "GOOGL",
    "amazon": "AMZN",
    "meta": "META",
    "facebook": "META",
    "amd": "AMD",
    "palantir": "PLTR",
    "sofi": "SOFI",
    "super micro": "SMCI",
    "broadcom": "AVGO",
    "intel": "INTC",
    # Media/Retail
    "netflix": "NFLX",
    "disney": "DIS",
    "costco": "COST",
    "walmart": "WMT",
    "target": "TGT",
    # Finance
    "jpmorgan": "JPM",
    "jp morgan": "JPM",
    "chase": "JPM",
    "goldman sachs": "GS",
    "wells fargo": "WFC",
    "bank of america": "BAC",
    "morgan stanley": "MS",
    "citigroup": "C",
    "citi": "C",
    # Industrial / Energy / Defense
    "boeing": "BA",
    "lockheed": "LMT",
    "raytheon": "RTX",
    "exxon": "XOM",
    "exxon mobil": "XOM",
    "exxonmobil": "XOM",
    "chevron": "CVX",
    "conocophillips": "COP",
    "3m": "MMM",
    "honeywell": "HON",
    "caterpillar": "CAT",
    "general electric": "GE",
    "ge aerospace": "GE",
    # Tech/Software
    "coinbase": "COIN",
    "robinhood": "HOOD",
    "uber": "UBER",
    "airbnb": "ABNB",
    "snowflake": "SNOW",
    "crowdstrike": "CRWD",
    "salesforce": "CRM",
    "adobe": "ADBE",
    "oracle": "ORCL",
    "servicenow": "NOW",
    "palo alto": "PANW",
    # Crypto
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    # Indices
    "s&p 500": "SPY",
    "s&p": "SPY",
    "dow jones": "DIA",
    "nasdaq": "QQQ",
    # Healthcare
    "unitedhealth": "UNH",
    "johnson & johnson": "JNJ",
    "j&j": "JNJ",
    "pfizer": "PFE",
    "eli lilly": "LLY",
    "abbvie": "ABBV",
    # Semiconductor
    "arm holdings": "ARM",
    "marvell": "MRVL",
    "micron": "MU",
    "qualcomm": "QCOM",
    "texas instruments": "TXN",
    "taiwan semi": "TSM",
    "tsmc": "TSM",
}


async def _translate_foreign_text(text: str, publisher: str) -> str:
    """Use the LLM to translate foreign text to English."""
    if not text or len(text.strip()) < 5:
        return text
    
    from app.services.prism_agent_caller import call_prism_agent
    
    prompt = f"You are a professional financial translator. Translate the following news snippet from {publisher} into English. Only return the English translation, no other text or explanation.\n\nText: {text}"
    
    try:
        reply, _, _ = await call_prism_agent(
            agent_id="translator",
            user_message=prompt,
            fallback_system_prompt="You are a professional financial translator. Output only the requested translation.",
            fallback_agent_name="Translator",
            temperature=0.1,
            max_tokens=1000,
        )
        if reply and len(reply.strip()) > 5:
            return reply.strip()
    except Exception as e:
        logger.warning("[news] Translation failed for %s: %s", publisher, e)
    
    return text


async def _scrape_article_body_via_service(url: str, max_chars: int = 15000) -> str:
    """Scrape article body using the auto engine on scraper-service."""
    from app.services.scraper_client import scraper_client

    res = await scraper_client.scrape(url, engine="auto", options={"max_chars": max_chars})
    if res and res.get("success") and res.get("content"):
        return res["content"]
    return ""


async def _scrape_with_timeout(url: str, fallback_summary: str, timeout: float = 15.0) -> str:
    """Scrape article body with a strict timeout, falling back to the API summary."""
    try:
        body = await asyncio.wait_for(_scrape_article_body_via_service(url), timeout=timeout)
        if body:
            return body
    except asyncio.TimeoutError:
        logger.warning("[news] Scrape timeout (15s) for URL: %s, falling back to API summary", url)
    except Exception as e:
        logger.warning("[news] Scrape failed for URL %s: %s, falling back to API summary", url, e)
    return fallback_summary


def _extract_text_from_html(html: str, max_chars: int = 15000) -> str:
    """Extract readable text from HTML using BeautifulSoup with a regex fallback."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for script in soup(["script", "style"]):
            script.decompose()
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = " ".join(chunk for chunk in chunks if chunk)
        return text[:max_chars]
    except Exception:
        # Simple regex fallback
        text = re.sub(r"<script.*?>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style.*?>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]


async def _detect_tickers_in_text(text: str) -> set[str]:
    """Detect stock tickers mentioned in article text."""
    symbols = await get_ticker_symbols(text)
    return set(symbols)



def _is_analyst_only_reference(ticker: str, text: str) -> bool:
    """Check if the ticker is a financial institution that is only mentioned
    as an analyst or rating agency, rather than being the subject of the article.
    """
    analyst_map = {
        "BAC": ["bank of america", "bofa"],
        "JPM": ["jpmorgan", "jp morgan", "chase"],
        "GS": ["goldman sachs", "goldman"],
        "MS": ["morgan stanley"],
        "C": ["citigroup", "citi"],
        "WFC": ["wells fargo"]
    }
    if ticker not in analyst_map:
        return False
        
    aliases = analyst_map[ticker]
    text_lower = text.lower()
    
    # If the text has $TICKER (e.g. $BAC), it's explicitly about the financial instrument
    if re.search(rf"\${re.escape(ticker)}\b", text_lower):
        return False
        
    # If the text directly talks about the bank's own stock/shares/earnings
    own_stock_patterns = [
        rf"\b{re.escape(ticker)}\s+(stock|shares|earnings|dividend|results|equity|assets|debt|ceo|valuation)\b",
        rf"{re.escape(ticker)}\b.*?\b(report|announced|released|posted)\b.*?\b(earnings|revenue|profit|income)\b",
    ]
    for alias in aliases:
        own_stock_patterns.append(rf"\b{re.escape(alias)}\s+(stock|shares|earnings|dividend|results|equity|assets|debt|ceo|valuation)\b")
        own_stock_patterns.append(rf"\b{re.escape(alias)}'s\s+(stock|shares|earnings|dividend|results|equity)\b")
        
    for pattern in own_stock_patterns:
        if re.search(pattern, text_lower):
            return False
            
    # Check if all occurrences of the aliases/names are followed or preceded by analyst keywords
    analyst_kws = [
        "analyst", "securities", "equity research", "strategist", "economist", 
        "brokerage", "firm", "research", "note", "report", "index", "upgrades", 
        "downgrades", "rating", "price target", "target price"
    ]
    
    has_any_mention = False
    all_are_analyst_refs = True
    
    for alias in aliases:
        for m in re.finditer(rf"\b{re.escape(alias)}\b", text_lower):
            has_any_mention = True
            # Check a window of 4 words after and before the match
            start_idx = max(0, m.start() - 50)
            end_idx = min(len(text_lower), m.end() + 50)
            window = text_lower[start_idx:end_idx]
            
            # Check if any analyst keyword is in this local window
            if not any(kw in window for kw in analyst_kws):
                all_are_analyst_refs = False
                break
        if not all_are_analyst_refs:
            break
            
    if has_any_mention and all_are_analyst_refs:
        return True
        
    return False


def _is_article_relevant_to_ticker(ticker: str, text: str) -> bool:
    """Check if an article actually discusses a ticker as a financial instrument.

    For short tickers (2-3 chars) that are also common English words (TV, HD, PC, etc.),
    we require additional evidence that the article is actually about the STOCK,
    not just using the letters as an abbreviation.

    Returns True if the article passes the relevance check.
    """
    if _is_analyst_only_reference(ticker, text):
        return False

    # Long tickers (4+ chars) and $TICKER syntax are inherently less ambiguous
    if len(ticker) >= 4:
        return True

    # If the article text contains the $TICKER pattern, it's explicitly financial
    if re.search(rf"\${re.escape(ticker)}\b", text):
        return True

    # Check if the company name is mentioned (not just the ticker letters)
    from app.processors.ticker_extractor import get_registry
    registry = get_registry()
    company = registry.lookup_symbol(ticker)
    if company:
        # Check for company name (e.g., "Grupo Televisa" for TV, "Home Depot" for HD)
        name_lower = company.name.lower()
        text_lower = text.lower()
        if name_lower in text_lower:
            return True
        # Check aliases
        for alias in company.aliases:
            if len(alias) > 3 and alias.lower() in text_lower:
                return True

    # For 2-3 letter tickers without company name: require strong financial context
    # near the ticker mention (at least 2 financial keywords within 150 chars)
    financial_kw = {
        "stock", "shares", "price", "earnings", "revenue", "profit",
        "bullish", "bearish", "analyst", "upgrade", "downgrade", "rating",
        "dividend", "ipo", "merger", "acquisition", "guidance", "forecast",
        "quarterly", "eps", "valuation", "rally", "surge", "plunge",
        "overweight", "underweight", "outperform", "underperform",
        "market cap", "pe ratio", "share price", "ticker",
    }
    for m in re.finditer(rf"\b{re.escape(ticker)}\b", text):
        start_idx = max(0, m.start() - 150)
        end_idx = min(len(text), m.end() + 150)
        window = text[start_idx:end_idx].lower()
        hits = sum(1 for kw in financial_kw if kw in window)
        if hits >= 2:
            return True

    return False


def _normalize_title(title: str) -> str:
    """Normalize title for cross-source deduplication."""
    t = title.lower().strip()
    t = re.sub(r"^(breaking|update|exclusive|report|analysis|opinion)[:\s-]+", "", t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()[:200]


def _get_article_id(title: str, ticker: str | None) -> str:
    """Generate a deterministic SHA256 hash ID for cross-source deduplication."""
    norm = _normalize_title(title)
    return hashlib.sha256(f"{norm}_{ticker or 'NONE'}".encode()).hexdigest()


def url_fanout_exceeded(db, url: str | None, cap: int | None = None) -> bool:
    """True when `url` already has >= cap news_articles rows.

    Per-ticker fan-out of one article is deliberate (all retrieval is
    ticker-keyed), but unbounded it stored one market-wide CNBC story 110×
    (58.6% of the table was duplicate-URL rows, audit 2026-07-23). Copies past
    the cap add storage/embedding cost and quality-assessor load with no new
    information. Fails open — a count error must never block collection.
    """
    if not url:
        return False
    if cap is None:
        from app.config import settings
        cap = getattr(settings, "NEWS_URL_FANOUT_CAP", 5)
    if cap <= 0:
        return False
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM news_articles WHERE url = %s", [url]
        ).fetchone()
        return bool(row and row[0] >= cap)
    except Exception:
        return False


def safe_emit(emit_cb, step: str, detail: str, status: str = "ok"):
    if not emit_cb:
        return
    try:
        import inspect
        sig = inspect.signature(emit_cb)
        params = list(sig.parameters.values())
        if len(params) >= 4:
            emit_cb("discovery", step, detail, status=status)
        else:
            emit_cb(step, detail, status)
    except Exception:
        pass


async def collect_feed(feed_name: str, feed_url: str, emit_cb: any = None, is_foreign: bool = False) -> int:
    """
    Fetch and parse a single RSS feed via scraper-service, write articles to news_articles.
    Returns number of new articles written.
    """
    from app.services.scraper_client import scraper_client

    count = 0
    try:
        with get_db() as db:
            items = await scraper_client.collect(
                source="news",
                req_data={
                    "feed_url": feed_url,
                    "query": feed_name,
                }
            )

            async def process_rss_article(article):
                title = article.get("title", "").strip()
                if not title:
                    return []

                url = article.get("url", "")
                summary = article.get("summary", "").strip()
                publisher = article.get("publisher", feed_name)

                # Translate if foreign
                if is_foreign:
                    title = await _translate_foreign_text(title, publisher)
                    if summary:
                        summary = await _translate_foreign_text(summary, publisher)

                pub_val = article.get("published_at")
                if isinstance(pub_val, str):
                    published_at = datetime.datetime.fromisoformat(pub_val)
                    if published_at.tzinfo is None:
                        published_at = published_at.replace(tzinfo=datetime.UTC)
                else:
                    published_at = datetime.datetime.now(datetime.UTC)

                # STRICT QUALITY GATE & BODY SCRAPING
                api_summary = summary
                summary = ""
                if url and (len(api_summary) < 150 or "..." in api_summary):
                    body = await _scrape_article_body_via_service(url)
                    if body and len(body) >= 150:
                        summary = body

                if not summary:
                    summary = api_summary

                if is_truncated_content(summary):
                    _note_drop("collect_feed", title, feed_name, len(summary))
                    return []

                from app.processors.dedup_engine import DedupEngine
                dedup = DedupEngine(table="news_articles")
                if dedup.is_duplicate(title, summary):
                    return []
                content_hash = dedup.compute_hash(title, summary)

                # Detect tickers in title + summary
                full_text = f"{title} {summary}"
                detected_tickers = await _detect_tickers_in_text(full_text)

                # Relevance gate: for short/ambiguous tickers, verify the article
                # actually discusses the stock (not just uses the letters as English).
                if detected_tickers:
                    relevant_tickers = {
                        t for t in detected_tickers
                        if _is_article_relevant_to_ticker(t, full_text)
                    }
                    detected_tickers = relevant_tickers

                res_items = []
                if detected_tickers:
                    for ticker in detected_tickers:
                        ticker_article_id = _get_article_id(title, ticker)
                        res_items.append({
                            "id": ticker_article_id,
                            "ticker": ticker,
                            "title": title,
                            "publisher": publisher,
                            "url": url,
                            "published_at": published_at,
                            "summary": summary,
                            "content_hash": content_hash,
                            "is_general": False,
                            "tickers_list": list(detected_tickers),
                        })
                else:
                    article_id = _get_article_id(title, None)
                    res_items.append({
                        "id": article_id,
                        "ticker": None,
                        "title": title,
                        "publisher": publisher,
                        "url": url,
                        "published_at": published_at,
                        "summary": summary,
                        "content_hash": content_hash,
                        "is_general": True,
                        "tickers_list": [],
                    })
                return res_items

            # Process up to 15 items in parallel to speed up RSS sweeps
            tasks = [process_rss_article(art) for art in items[:15]]
            results_lists = await asyncio.gather(*tasks)

            for item_list in results_lists:
                if not item_list:
                    continue
                for item in item_list:
                    if url_fanout_exceeded(db, item.get("url")):
                        continue
                    _qs, _qr = quality_at_write(item["title"], item["summary"])
                    db.execute(
                        """
                        INSERT INTO news_articles
                        (id, ticker, title, publisher, url, published_at, summary, source, content_hash, collected_at, quality_status, quality_reason)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'rss', %s, CURRENT_TIMESTAMP, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        [
                            item["id"],
                            item["ticker"],
                            item["title"][:500],
                            item["publisher"],
                            item["url"],
                            item["published_at"],
                            item["summary"],
                            item["content_hash"],
                            _qs,
                            _qr,
                        ],
                    )
                    count += 1

                # Emit news scraped log for this unique item list
                first_item = item_list[0]
                if not first_item.get("is_general"):
                    safe_emit(
                        emit_cb,
                        "news_scraped",
                        f"📰 {first_item['publisher']}: '{first_item['title'][:80]}' -> Extracted: {first_item['tickers_list']}",
                        status="ok"
                    )
                else:
                    safe_emit(
                        emit_cb,
                        "news_scraped",
                        f"📰 {first_item['publisher']}: '{first_item['title'][:80]}' -> Extracted: General",
                        status="ok"
                    )
    except Exception as e:
        logger.error(f"[news] {feed_name} FAILED: {type(e).__name__}: {e}", exc_info=True)

    return count


async def collect_all(limit_feeds: int | None = None, emit_cb: any = None) -> int:
    """Fetch all RSS feeds. Returns total articles written."""
    total = 0
    failed = 0
    
    # Combine regular and foreign feeds
    feeds_to_check = [(name, url, False) for name, url in RSS_FEEDS.items()]
    feeds_to_check += [(name, url, True) for name, url in FOREIGN_RSS_FEEDS.items()]
    
    if limit_feeds and limit_feeds > 0 and limit_feeds < len(feeds_to_check):
        feeds_to_check = feeds_to_check[:limit_feeds]

    for name, url, is_foreign in feeds_to_check:
        try:
            count = await collect_feed(name, url, emit_cb=emit_cb, is_foreign=is_foreign)
            if count > 0:
                logger.info(f"[news] {name} (Foreign={is_foreign}): {count} articles")
            total += count
        except Exception as e:
            failed += 1
            logger.error(
                f"[news] {name}: UNCAUGHT: {type(e).__name__}: {e}",
                exc_info=True,
            )
        await asyncio.sleep(2.0)

    logger.info(
        f"[news] Total: {total} articles from {len(feeds_to_check)} feeds"
        + (f" ({failed} failed)" if failed else "")
    )
    return total


async def collect_for_ticker(ticker: str, since: datetime.datetime | None = None) -> int:
    """Collect news articles mentioning a specific ticker."""
    total = 0

    # Layer 1: Finnhub (highest volume, most reliable)
    fh_count = await collect_finnhub_news(ticker, since=since)
    total += fh_count
    await asyncio.sleep(3)

    # Layer 2: yfinance headlines
    yf_count = await collect_yfinance_news(ticker, since=since)
    total += yf_count

    logger.info(
        f"[news] {ticker}: {total} total articles (finnhub={fh_count}, yfinance={yf_count})"
    )
    return total


async def collect_finnhub_news(
    ticker: str, days: int = 7, max_articles: int = 15, since: datetime.datetime | None = None, emit_cb: any = None
) -> int:
    """Fetch per-ticker news from Finnhub API."""
    import os

    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        logger.info("[news] FINNHUB_API_KEY not set, skipping Finnhub")
        return 0

    try:
        import finnhub
    except ImportError:
        logger.info("[news] finnhub-python not installed, skipping")
        return 0

    try:
        client = finnhub.Client(api_key=api_key)
        end = datetime.datetime.now()
        start = end - datetime.timedelta(days=days)
        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")

        news = await asyncio.to_thread(
            client.company_news, ticker, _from=start_str, to=end_str
        )

        if not news:
            return 0

        news.sort(key=lambda a: a.get("datetime", 0), reverse=True)

        with get_db() as db:
            trusted = db.execute("SELECT source_name, win_rate, total_items FROM source_trust WHERE source_type='publisher'").fetchall()
        bad_publishers = {row[0] for row in trusted if row[2] >= 5 and row[1] < 0.1}

        from app.processors.dedup_engine import DedupEngine
        dedup = DedupEngine(table="news_articles", ticker=ticker)
        unique_articles = []
        skipped = 0

        for article in news:
            source = article.get("source", "")
            if source in bad_publishers:
                skipped += 1
                continue

            headline = article.get("headline", "").strip()
            summary = article.get("summary", "").strip()
            if not headline:
                continue

            if dedup.is_duplicate(headline, summary):
                skipped += 1
                continue

            unique_articles.append(article)
            if len(unique_articles) >= max_articles:
                break

        async def process_article(article):
            headline = article.get("headline", "").strip()
            summary = article.get("summary", "").strip()
            url = article.get("url", "")
            source = article.get("source", "finnhub")
            ts = article.get("datetime", 0)

            # NOTE: Body scraping removed from collection phase to fix 120s timeouts.
            # Full article bodies are fetched lazily via deep_read_top_articles()
            # during the analysis phase. Store with API summary for now.
            if not summary:
                summary = headline  # Use headline as minimal fallback

            if is_truncated_content(summary):
                _note_drop("finnhub", headline, source, len(summary))
                return []

            published_at = (
                datetime.datetime.fromtimestamp(ts, tz=datetime.UTC) if ts else None
            )

            if since and published_at and published_at <= since:
                return []

            full_text = f"{headline} {summary}"
            detected_tickers = await _detect_tickers_in_text(full_text)
            if detected_tickers:
                detected_tickers = {
                    t for t in detected_tickers
                    if _is_article_relevant_to_ticker(t, full_text)
                }
            tickers_to_insert = list(detected_tickers) if detected_tickers else [ticker.upper()]

            from app.processors.dedup_engine import DedupEngine
            dedup = DedupEngine(table="news_articles")
            content_hash = dedup.compute_hash(headline, summary)

            res = []
            for t in tickers_to_insert:
                article_id = _get_article_id(headline, t)
                res.append({
                    "id": article_id,
                    "ticker": t,
                    "title": headline,
                    "publisher": source,
                    "url": url,
                    "published_at": published_at,
                    "summary": summary,
                    "content_hash": content_hash,
                })
            safe_emit(
                emit_cb,
                "news_scraped",
                f"📰 Finnhub: '{headline[:80]}' -> Extracted: {tickers_to_insert}",
                status="ok"
            )
            return res

        # Run concurrent scraping, ticker extraction, and relevance gating
        tasks = [process_article(art) for art in unique_articles]
        results_lists = await asyncio.gather(*tasks)

        with get_db() as db:
            count = 0
            for item_list in results_lists:
                for item in item_list:
                    if url_fanout_exceeded(db, item.get("url")):
                        continue
                    _qs, _qr = quality_at_write(item["title"], item["summary"])
                    db.execute(
                        """
                        INSERT INTO news_articles
                        (id, ticker, title, publisher, url, published_at, summary, source, content_hash, collected_at, quality_status, quality_reason)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'finnhub', %s, CURRENT_TIMESTAMP, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        [
                            item["id"],
                            item["ticker"],
                            item["title"][:500],
                            item["publisher"],
                            item["url"],
                            item["published_at"],
                            item["summary"],
                            item.get("content_hash"),
                            _qs,
                            _qr,
                        ],
                    )
                    count += 1

        logger.info(
            f"[news] Finnhub {ticker}: {count} unique articles (skipped {skipped} duplicates)"
        )
        await asyncio.sleep(1)
        return count

    except Exception as e:
        logger.info(f"[news] Finnhub {ticker} error: {e}")
        return 0


async def collect_yfinance_news(ticker: str, since: datetime.datetime | None = None) -> int:
    """Fetch per-ticker news from yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        logger.info("[news] yfinance not installed, skipping")
        return 0

    try:
        t = yf.Ticker(ticker)
        news = await asyncio.to_thread(lambda: t.news)

        if not news:
            return 0

        with get_db() as db:
            trusted = db.execute("SELECT source_name, win_rate, total_items FROM source_trust WHERE source_type='publisher'").fetchall()
        bad_publishers = {row[0] for row in trusted if row[2] >= 5 and row[1] < 0.1}

        # Helper to process a single yfinance article
        async def process_yf_article(article):
            content = article.get("content", article)
            title = content.get("title", "").strip()
            if not title:
                return []

            from app.processors.dedup_engine import DedupEngine
            dedup = DedupEngine(table="news_articles", ticker=ticker)
            api_summary = content.get("description", "") or content.get("summary", "")
            if dedup.is_duplicate(title, api_summary):
                return []

            url = ""
            if "canonicalUrl" in content:
                url_obj = content["canonicalUrl"]
                url = url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj)
            elif "clickThroughUrl" in content:
                url_obj = content["clickThroughUrl"]
                url = url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj)
            elif "link" in article:
                url = article["link"]

            provider = content.get("provider", {})
            publisher = (
                provider.get("displayName", "yfinance")
                if isinstance(provider, dict)
                else "yfinance"
            )

            if publisher in bad_publishers:
                return []

            pub_date = content.get("pubDate", "")
            published_at = None
            if pub_date:
                try:
                    published_at = datetime.datetime.fromisoformat(
                        pub_date.replace("Z", "+00:00")
                    )
                except Exception:
                    pass

            api_summary = content.get("description", "") or content.get("summary", "")
            # NOTE: Body scraping removed from collection phase to fix 120s timeouts.
            # Full article bodies are fetched lazily via deep_read_top_articles()
            # during the analysis phase. Store with API summary for now.
            summary = api_summary
            if not summary:
                summary = title  # Use title as minimal fallback

            if is_truncated_content(summary):
                _note_drop("yfinance", title, publisher, len(summary))
                return []

            if since and published_at and published_at <= since:
                return []

            full_text = f"{title} {summary}"
            detected_tickers = await _detect_tickers_in_text(full_text)
            if detected_tickers:
                detected_tickers = {
                    t for t in detected_tickers
                    if _is_article_relevant_to_ticker(t, full_text)
                }
            tickers_to_insert = list(detected_tickers) if detected_tickers else [ticker.upper()]

            from app.processors.dedup_engine import DedupEngine
            dedup = DedupEngine(table="news_articles")
            content_hash = dedup.compute_hash(title, summary)

            res = []
            for t in tickers_to_insert:
                article_id = _get_article_id(title, t)
                res.append({
                    "id": article_id,
                    "ticker": t,
                    "title": title,
                    "publisher": publisher,
                    "url": url,
                    "published_at": published_at,
                    "summary": summary,
                    "content_hash": content_hash,
                })
            return res

        # Run concurrent scraping, ticker extraction, and relevance gating
        tasks = [process_yf_article(art) for art in news]
        results_lists = await asyncio.gather(*tasks)

        with get_db() as db:
            count = 0
            for item_list in results_lists:
                for item in item_list:
                    if url_fanout_exceeded(db, item.get("url")):
                        continue
                    _qs, _qr = quality_at_write(item["title"], item["summary"])
                    db.execute(
                        """
                        INSERT INTO news_articles
                        (id, ticker, title, publisher, url, published_at, summary, source, content_hash, collected_at, quality_status, quality_reason)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'yfinance', %s, CURRENT_TIMESTAMP, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        [
                            item["id"],
                            item["ticker"],
                            item["title"][:500],
                            item["publisher"],
                            item["url"],
                            item["published_at"],
                            item["summary"],
                            item.get("content_hash"),
                            _qs,
                            _qr,
                        ],
                    )
                    count += 1

            logger.info(f"[news] yfinance {ticker}: {count} articles")
            return count

    except Exception as e:
        logger.info(f"[news] yfinance {ticker} error: {e}")
        return 0


_GARBAGE_STRINGS = [
    "Accessibility Menu", "Skip to main content", "Skip to Content", "Sign in / Join",
    "Premium Investing Services", "Stock Advisor", "Rule Breakers", "Join Stock Advisor",
    "Subscribe Now", "Motley Fool", "Accept All Cookies", "Cookie Settings", "Privacy Policy",
    "We and our partners", "consent to the use", "strictly necessary", "Toggle navigation",
    "Open Navigation", "Close Navigation", "Full Screen Menu", "Site Navigation", "Main Navigation",
]


def _clean_deep_read(text: str) -> str | None:
    """Strip known garbage strings from deep-read content."""
    if not text:
        return None

    original_len = len(text)
    cleaned = text

    for g in _GARBAGE_STRINGS:
        cleaned = cleaned.replace(g, "")

    lines = cleaned.split("\n")
    start_cut = 0
    for line in lines:
        stripped = line.strip()
        if len(stripped) < 20 and start_cut < 10:
            start_cut += 1
        else:
            break
    lines = lines[start_cut:]
    cleaned = "\n".join(lines).strip()

    if original_len > 0 and len(cleaned) < original_len * 0.5:
        return None

    if len(cleaned) < 100:
        return None

    return cleaned


async def deep_read_article(url: str, max_chars: int = 15000) -> str | None:
    """Deep-read a news article URL for full article body."""
    # Method 0: Adaptive Scraper
    try:
        from app.collectors.adaptive_scraper import run_adaptive

        adaptive_text = await run_adaptive(url)
        if adaptive_text and len(adaptive_text) > 100:
            cleaned = _clean_deep_read(adaptive_text[:max_chars])
            if cleaned:
                logger.info(f"[news] adaptive-read: {len(cleaned)} chars from {url[:50]}")
                return cleaned
        logger.info("[news] deep-read: adaptive scraper failed, trying crawl4ai...")
    except Exception as e:
        logger.info(f"[news] adaptive-read error for {url[:50]}: {e}")

    # Method 1: crawl4ai via scraper-service
    try:
        from app.services.scraper_client import scraper_client
        res = await scraper_client.scrape(url, engine="crawl4ai", options={"max_chars": max_chars})
        if res and res.get("success") and res.get("content"):
            text = res["content"]
            if len(text) > 100 and "oops" not in text.lower()[:50]:
                cleaned = _clean_deep_read(text)
                if cleaned:
                    logger.info(f"[news] deep-read (crawl4ai): {len(cleaned)} chars from {url[:50]}")
                    return cleaned
            logger.info("[news] deep-read: crawl4ai got placeholder, trying vision...")
    except Exception as e:
        logger.info(f"[news] deep-read crawl4ai error for {url[:50]}: {e}")

    # Method 2: Vision pipeline via scraper-service
    try:
        from app.services.scraper_client import scraper_client
        res = await scraper_client.scrape(url, engine="vision", options={"max_chars": max_chars})
        if res and res.get("success") and res.get("content"):
            text = res["content"]
            if text and len(text) > 100:
                cleaned = _clean_deep_read(text[:max_chars])
                if cleaned:
                    logger.info(f"[news] vision deep-read: {len(cleaned)} chars from {url[:50]}")
                    return cleaned
    except Exception as e:
        logger.info(f"[news] vision deep-read error for {url[:50]}: {e}")

    return None


async def deep_read_top_articles(
    ticker: str, limit: int = 3, max_chars: int = 15000
) -> list[dict]:
    """Deep-read the top N most recent articles for a ticker."""
    with get_db() as db:
        articles = db.execute(
            """
            SELECT id, title, url, summary FROM news_articles
            WHERE ticker = %s AND url != '' AND url IS NOT NULL
            ORDER BY published_at DESC
            LIMIT %s
        """,
            [ticker.upper(), limit * 2],
        ).fetchall()

        results = []
        for row in articles:
            if len(results) >= limit:
                break

            article_id, title, url, summary = row

            if summary and len(summary) > 200:
                results.append({"title": title, "url": url, "full_text": summary})
                continue

            full_text = await deep_read_article(url, max_chars)
            if full_text:
                # Never store a bot-wall/captcha page as the article body — this
                # UPDATE was the main path that wrote block pages into summaries.
                if is_scrape_artifact(full_text):
                    db.execute(
                        "UPDATE news_articles SET quality_status = 'discarded', "
                        "quality_reason = 'scrape_artifact_deep_read' WHERE id = %s",
                        [article_id],
                    )
                    logger.info(f"[news] Deep-read {ticker}: scrape artifact for {url} — flagged, not stored")
                    continue
                db.execute(
                    "UPDATE news_articles SET summary = %s WHERE id = %s",
                    [full_text, article_id],
                )
                results.append({"title": title, "url": url, "full_text": full_text})

            await asyncio.sleep(5)

        logger.info(f"[news] Deep-read {ticker}: {len(results)} articles with full text")
        return results
