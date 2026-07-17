"""
Collect routes — High-level data collection from Reddit, YouTube, News/RSS, and Forums.
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.scraper.api.schemas import CollectRequest, CollectResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/collect")
async def collect(req: CollectRequest):
    """Collect data from a specified source.

    Sources:
      - reddit: Posts from subreddits (requires subreddits list)
      - youtube: Video transcripts (requires channels or query)
      - news/rss: Articles from RSS feeds (requires feed_url or feeds dict)
      - discourse: Posts from Discourse forums (requires base_url)
      - xenforo: Posts from XenForo forums (requires base_url)

    All domain context (which subreddits, which feeds, which keywords)
    comes from the caller — this service has zero domain knowledge.
    """
    try:
        if req.stream:
            if req.source == "youtube":
                return await _collect_youtube_stream(req)
            else:
                return await _collect_fallback_stream(req)

        if req.source == "reddit":
            return await _collect_reddit(req)
        elif req.source == "reddit-purge":
            return await _collect_reddit_purge(req)
        elif req.source == "youtube":
            return await _collect_youtube(req)
        elif req.source in ("news", "rss"):
            return await _collect_news(req)
        elif req.source == "discourse":
            return await _collect_discourse(req)
        elif req.source == "xenforo":
            return await _collect_xenforo(req)
        elif req.source == "kannapedia":
            return await _collect_kannapedia(req)
        elif req.source == "leafly":
            return await _collect_leafly(req)
        elif req.source == "duckduckgo":
            return await _collect_duckduckgo(req)
        elif req.source == "twitter":
            return await _collect_twitter(req)
        elif req.source == "stocktwits":
            return await _collect_stocktwits(req)
        elif req.source == "finnews":
            return await _collect_finnews(req)
        else:
            return CollectResponse(
                source=req.source, count=0, items=[],
                error=f"Unknown source: {req.source}",
            )
    except Exception as e:
        logger.error(f"[collect] {req.source} error: {e}", exc_info=True)
        return CollectResponse(
            source=req.source, count=0, items=[],
            error=str(e),
        )


async def _collect_reddit_purge(req: CollectRequest) -> CollectResponse:
    """Collect Reddit posts, extract and validate ticker symbols."""
    import os
    from app.scraper.collectors.reddit_purge_collector import RedditPurgeCollector

    collector = RedditPurgeCollector()
    ollama_host = req.ollama_host or os.getenv("PRISM_URL", "http://lazy-tool-service:7778/agent")
    ollama_model = req.ollama_model or os.getenv("PURGE_MODEL", "vllm/cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit")
    
    ticker_results = await collector.collect(
        subreddits=req.subreddits,
        use_llm=req.use_llm,
        ollama_host=ollama_host,
        ollama_model=ollama_model,
        limit=req.limit or 10
    )
    
    return CollectResponse(source="reddit-purge", count=len(ticker_results), items=ticker_results)


async def _collect_reddit(req: CollectRequest) -> CollectResponse:
    """Collect Reddit posts."""
    from app.scraper.collectors.reddit_collector import RedditCollector, _serialize_post

    if not req.subreddits:
        return CollectResponse(
            source="reddit", count=0, items=[],
            error="subreddits list is required for reddit collection",
        )

    collector = RedditCollector()

    if req.query:
        # Search mode
        posts = await collector.search(
            query=req.query,
            subreddits=req.subreddits,
            limit=req.limit,
            time_filter=req.time_filter or "all",
        )
    else:
        # General sweep mode
        posts = await collector.get_posts(
            subreddits=req.subreddits,
            limit=req.limit,
            keywords=req.keywords,
            sort=req.sort or "hot",
            time_filter=req.time_filter or "day",
        )

    items = [_serialize_post(p) for p in posts]
    return CollectResponse(source="reddit", count=len(items), items=items)


async def _collect_youtube(req: CollectRequest) -> CollectResponse:
    """Collect YouTube video transcripts."""
    from app.scraper.collectors.youtube_collector import YouTubeCollector, _serialize_video

    collector = YouTubeCollector()
    all_videos = []

    if req.channels:
        # Channel mode
        for channel in req.channels:
            videos = await collector.collect_channel(
                channel_handle=channel,
                max_videos=min(req.limit, 10),
                days_back=req.days_back if req.days_back is not None else 0,
                require_transcript=req.require_transcript,
            )
            all_videos.extend(videos)
    elif req.query:
        # Search mode
        days_back = req.days_back if req.days_back is not None else 0
        all_videos = await collector.search(
            query=req.query,
            max_results=req.limit,
            days_back=days_back,
            require_transcript=req.require_transcript,
            sort=req.sort,
            offset=req.offset,
        )
    else:
        return CollectResponse(
            source="youtube", count=0, items=[],
            error="Either 'channels' or 'query' is required for youtube collection",
        )

    items = [_serialize_video(v) for v in all_videos]
    return CollectResponse(source="youtube", count=len(items), items=items)


async def _collect_news(req: CollectRequest) -> CollectResponse:
    """Collect news articles from RSS feeds."""
    from app.scraper.collectors.news_collector import NewsCollector, _serialize_article

    collector = NewsCollector()

    if req.feeds:
        # Multi-feed mode
        articles = await collector.collect_feeds(feeds=req.feeds)
    elif req.feed_url:
        # Single feed mode
        feed_name = req.query or "feed"
        articles = await collector.collect_feed(feed_name, req.feed_url)
    else:
        return CollectResponse(
            source="news", count=0, items=[],
            error="Either 'feed_url' or 'feeds' dict is required for news collection",
        )

    # Apply keyword filter if provided
    if req.keywords:
        filtered = []
        for a in articles:
            text = f"{a.title} {a.summary}".lower()
            if any(kw.lower() in text for kw in req.keywords):
                filtered.append(a)
        articles = filtered

    # Apply limit
    articles = articles[:req.limit]

    items = [_serialize_article(a) for a in articles]
    return CollectResponse(source="news", count=len(items), items=items)


async def _collect_discourse(req: CollectRequest) -> CollectResponse:
    """Collect posts from a Discourse forum (e.g. Overgrow)."""
    from app.scraper.collectors.discourse_collector import DiscourseCollector, _serialize_forum_post

    if not req.base_url:
        return CollectResponse(
            source="discourse", count=0, items=[],
            error="base_url is required for discourse collection (e.g. https://overgrow.com)",
        )

    collector = DiscourseCollector(
        base_url=req.base_url,
        forum_name=req.forum_name or "discourse",
    )

    posts = []

    # If thread_url is provided, extract topic_id from it
    # URL format: https://overgrow.com/t/the-bank-of-stank/38516/1
    if req.thread_url and not req.topic_id:
        import re
        m = re.search(r'/t/[^/]+/(\d+)', req.thread_url)
        if m:
            req.topic_id = int(m.group(1))

    if req.topic_id:
        # Get all posts from a specific topic/thread
        posts = await collector.get_topic_posts(
            topic_id=req.topic_id,
            max_posts=req.limit,
        )
    elif req.query:
        # Search mode
        posts = await collector.search(
            query=req.query,
            category_slug=req.category_slug,
            tags=[req.tag] if req.tag else None,
            limit=req.limit,
        )
    elif req.tag:
        # Tag filter mode
        posts = await collector.get_topics_by_tag(
            tag=req.tag,
            limit=req.limit,
        )
    elif req.category_slug and req.category_id:
        # Category mode
        posts = await collector.get_category_topics(
            category_slug=req.category_slug,
            category_id=req.category_id,
            limit=req.limit,
        )
    elif req.period:
        # Top topics by period
        posts = await collector.get_top_topics(
            period=req.period,
            limit=req.limit,
        )
    else:
        # Default: latest topics
        posts = await collector.get_latest_topics(limit=req.limit)

    # Apply keyword filter if provided
    if req.keywords and posts:
        filtered = []
        for p in posts:
            text = f"{p.title} {p.body}".lower()
            if any(kw.lower() in text for kw in req.keywords):
                filtered.append(p)
        posts = filtered

    items = [_serialize_forum_post(p) for p in posts]
    return CollectResponse(source="discourse", count=len(items), items=items)


async def _collect_xenforo(req: CollectRequest) -> CollectResponse:
    """Collect posts from a XenForo forum (e.g. Rollitup, THCFarmer)."""
    from app.scraper.collectors.xenforo_collector import XenForoCollector, _serialize_xenforo_post

    if not req.base_url:
        return CollectResponse(
            source="xenforo", count=0, items=[],
            error="base_url is required for xenforo collection (e.g. https://www.rollitup.org)",
        )

    collector = XenForoCollector(
        base_url=req.base_url,
        forum_name=req.forum_name or "xenforo",
    )

    posts = []

    if req.thread_url:
        # Scrape all posts from a specific thread
        posts = await collector.get_thread_posts(
            thread_url=req.thread_url,
            max_posts=req.limit,
        )
    elif req.query:
        # Search mode
        posts = await collector.search(
            query=req.query,
            limit=req.limit,
        )
    elif req.subforum_path:
        # Subforum thread listing
        posts = await collector.get_forum_threads(
            subforum_path=req.subforum_path,
            limit=req.limit,
        )
    else:
        return CollectResponse(
            source="xenforo", count=0, items=[],
            error="One of 'thread_url', 'query', or 'subforum_path' is required",
        )

    # Apply keyword filter if provided
    if req.keywords and posts:
        filtered = []
        for p in posts:
            text = f"{p.title} {p.body}".lower()
            if any(kw.lower() in text for kw in req.keywords):
                filtered.append(p)
        posts = filtered

    items = [_serialize_xenforo_post(p) for p in posts]
    return CollectResponse(source="xenforo", count=len(items), items=items)


async def _collect_kannapedia(req: CollectRequest) -> CollectResponse:
    """Collect strain data from Kannapedia.
    
    Two modes:
      1. rsp_numbers provided → scrape those specific strains
      2. query provided → search the Kannapedia index for matching strain names,
         resolve to RSP numbers, then scrape
    """
    from app.scraper.collectors.kannapedia_collector import KannapediaCollector, _serialize_strain
    import re
    import httpx

    collector = KannapediaCollector()
    rsp_numbers = req.rsp_numbers or []

    # If query is provided but no RSP numbers, search Kannapedia index
    if req.query and not rsp_numbers:
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": "CannabisResearcher/1.0 (academic research)"},
            ) as client:
                resp = await client.get("https://kannapedia.net/strains")
                resp.raise_for_status()
                html = resp.text

            import difflib

            query_lower = req.query.strip().lower()
            q_norm = re.sub(r'[^a-z0-9]', '', query_lower)
            candidates = []
            # Match strain entries: <h2 ... data-name="..."> with <a href="/strains/rspXXXXX">
            for m in re.finditer(
                r'data-name="([^"]+)"[^>]*>\s*<a\s+href="/strains/(rsp\d+)"',
                html,
                re.IGNORECASE,
            ):
                strain_name = m.group(1).strip()
                rsp = m.group(2).strip()
                s_norm = re.sub(r'[^a-z0-9]', '', strain_name.lower())
                
                ratio = difflib.SequenceMatcher(None, q_norm, s_norm).ratio()
                
                score = 0
                if q_norm == s_norm:
                    score = 100
                elif s_norm.startswith(q_norm) or q_norm.startswith(s_norm):
                    score = 90
                elif q_norm in s_norm or s_norm in q_norm:
                    score = 80
                elif ratio >= 0.8:
                    score = int(ratio * 100)
                
                if score > 0:
                    candidates.append((score, rsp))
            
            # Sort candidates by score descending
            candidates.sort(key=lambda x: x[0], reverse=True)
            
            # De-duplicate RSPs while preserving order
            seen_rsps = set()
            for score, rsp in candidates:
                if rsp not in seen_rsps:
                    seen_rsps.add(rsp)
                    rsp_numbers.append(rsp)
                    if len(rsp_numbers) >= req.limit:
                        break
        except Exception as e:
            logger.error(f"[kannapedia] Failed to search index: {e}")
            return CollectResponse(
                source="kannapedia", count=0, items=[],
                error=f"Failed to search Kannapedia index: {e}",
            )

    if not rsp_numbers:
        return CollectResponse(
            source="kannapedia", count=0, items=[],
            error="No RSP numbers found. Provide rsp_numbers or a search query.",
        )

    # Scrape each RSP
    strains = await collector.get_strains(
        rsp_numbers[:req.limit],
        continue_on_error=True,
    )

    items = [_serialize_strain(s) for s in strains]
    return CollectResponse(source="kannapedia", count=len(items), items=items)


async def _collect_leafly(req: CollectRequest) -> CollectResponse:
    """Collect strain terpene profile from Leafly."""
    from app.scraper.collectors.leafly_collector import LeaflyCollector

    if not req.query:
        return CollectResponse(
            source="leafly", count=0, items=[],
            error="query is required for leafly collection",
        )

    collector = LeaflyCollector()
    data = await collector.get_strain(req.query)
    
    if not data:
        return CollectResponse(
            source="leafly", count=0, items=[],
            error=f"No Leafly data found for query: {req.query}",
        )

    return CollectResponse(
        source="leafly", count=1, items=[data]
    )


async def _collect_duckduckgo(req: CollectRequest) -> CollectResponse:
    """Collect search results from DuckDuckGo."""
    from app.scraper.collectors.duckduckgo_collector import DuckDuckGoCollector

    if not req.query:
        return CollectResponse(
            source="duckduckgo", count=0, items=[],
            error="query is required for duckduckgo collection",
        )

    collector = DuckDuckGoCollector()
    results = await collector.search(
        query=req.query,
        limit=req.limit,
        date_restrict=req.time_filter, # Optional parameter mapping
    )

    return CollectResponse(source="duckduckgo", count=len(results), items=results)


async def _collect_youtube_stream(req: CollectRequest) -> StreamingResponse:
    """Stream YouTube video transcripts or searches as NDJSON."""
    from app.scraper.collectors.youtube_collector import YouTubeCollector, _serialize_video
    import json

    collector = YouTubeCollector()

    async def event_generator():
        try:
            if req.channels:
                logger.info(f"[collect] YouTube stream started for channels: {req.channels}")
                for channel in req.channels:
                    async for video in collector.collect_channel_generator(
                        channel_handle=channel,
                        max_videos=min(req.limit, 10),
                        days_back=req.days_back if req.days_back is not None else 0,
                        require_transcript=req.require_transcript,
                    ):
                        logger.info(f"[collect] Yielding video {video.video_id} for channel {channel}")
                        yield json.dumps(_serialize_video(video)) + "\n"
            elif req.query:
                logger.info(f"[collect] YouTube stream started for search query: '{req.query}'")
                days_back = req.days_back if req.days_back is not None else 0
                async for video in collector.search_generator(
                    query=req.query,
                    max_results=req.limit,
                    days_back=days_back,
                    require_transcript=req.require_transcript,
                    sort=req.sort,
                    offset=req.offset,
                ):
                    logger.info(f"[collect] Yielding search result video {video.video_id} for query '{req.query}'")
                    yield json.dumps(_serialize_video(video)) + "\n"
                logger.info(f"[collect] YouTube stream finished for search query: '{req.query}'")
        except Exception as e:
            logger.error(f"[collect] youtube stream error: {e}", exc_info=True)
            yield json.dumps({"error": str(e)}) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


async def _collect_fallback_stream(req: CollectRequest) -> StreamingResponse:
    """Fallback: stream items from other collectors one-by-one as NDJSON."""
    import json
    async def event_generator():
        try:
            if req.source == "reddit":
                res = await _collect_reddit(req)
            elif req.source == "reddit-purge":
                res = await _collect_reddit_purge(req)
            elif req.source in ("news", "rss"):
                res = await _collect_news(req)
            elif req.source == "discourse":
                res = await _collect_discourse(req)
            elif req.source == "xenforo":
                res = await _collect_xenforo(req)
            elif req.source == "kannapedia":
                res = await _collect_kannapedia(req)
            elif req.source == "leafly":
                res = await _collect_leafly(req)
            elif req.source == "duckduckgo":
                res = await _collect_duckduckgo(req)
            elif req.source == "twitter":
                res = await _collect_twitter(req)
            elif req.source == "stocktwits":
                res = await _collect_stocktwits(req)
            elif req.source == "finnews":
                res = await _collect_finnews(req)
            else:
                yield json.dumps({"error": f"Unknown source: {req.source}"}) + "\n"
                return

            if res.error:
                yield json.dumps({"error": res.error}) + "\n"
            else:
                for item in res.items:
                    yield json.dumps(item) + "\n"
        except Exception as e:
            logger.error(f"[collect] fallback stream error: {e}", exc_info=True)
            yield json.dumps({"error": str(e)}) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


async def _collect_twitter(req: CollectRequest) -> CollectResponse:
    """Collect tweets using twscrape."""
    from app.scraper.collectors.twitter_collector import TwitterCollector, _serialize_tweet
    collector = TwitterCollector()
    tweets = []
    
    if req.cashtags:
        for tag in req.cashtags:
            results = await collector.get_cashtag_feed(tag, limit=req.limit)
            tweets.extend(results)
    elif req.usernames:
        for username in req.usernames:
            results = await collector.get_user_tweets(username, limit=req.limit)
            tweets.extend(results)
    elif req.query:
        tweets = await collector.search(req.query, limit=req.limit)
    else:
        return CollectResponse(source="twitter", count=0, items=[], 
                              error="One of 'cashtags', 'usernames', or 'query' is required")
    
    items = [_serialize_tweet(t) for t in tweets]
    return CollectResponse(source="twitter", count=len(items), items=items)


async def _collect_stocktwits(req: CollectRequest) -> CollectResponse:
    """Collect StockTwits messages."""
    from app.scraper.collectors.stocktwits_collector import StockTwitsCollector, _serialize_message
    if not req.symbol:
        return CollectResponse(source="stocktwits", count=0, items=[], error="Field 'symbol' is required for StockTwits collection")
    
    collector = StockTwitsCollector()
    messages = await collector.get_symbol_stream(req.symbol, limit=req.limit or 30)
    items = [_serialize_message(m) for m in messages]
    return CollectResponse(source="stocktwits", count=len(items), items=items)

async def _collect_finnews(req: CollectRequest) -> CollectResponse:
    """Collect financial news from API providers (Marketaux, Finnhub, AlphaVantage, etc.)."""
    from app.scraper.collectors.finnews_collector import FinNewsCollector, _serialize_article

    collector = FinNewsCollector()

    if req.provider:
        # Single provider mode
        articles = await collector.fetch(
            provider=req.provider,
            tickers=req.tickers,
            query=req.query,
            limit=req.limit,
            days_back=req.days_back or 7,
        )
    else:
        # All available providers
        articles = await collector.fetch_all(
            tickers=req.tickers,
            query=req.query,
            limit=req.limit,
            days_back=req.days_back or 7,
        )

    items = [_serialize_article(a) for a in articles]
    return CollectResponse(source="finnews", count=len(items), items=items)
