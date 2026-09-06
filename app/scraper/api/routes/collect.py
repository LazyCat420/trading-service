"""
Collect routes — High-level data collection from Reddit, YouTube, News/RSS, and Forums.
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.scraper.api.schemas import CollectRequest, CollectResponse
from app.scraper.core.url_guard import UnsafeUrlError, check_url

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/collect")
async def collect(req: CollectRequest):
    """Collect data from a specified source.

    Sources:
      - reddit: Posts from subreddits (requires subreddits list)
      - youtube: Video transcripts (requires channels or query)
      - news/rss: Articles from RSS feeds (requires feed_url or feeds dict)

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
                source=req.source, count=0, items=[], success=False,
                error=f"Unknown source: {req.source}",
            )
    except Exception as e:
        # Everything reaching here is a SERVER fault — a collector raised. The
        # response is still 200 (callers rely on that), but it must not look
        # like an empty result: success=False plus `error` is what lets
        # scraper_client tell an outage from a quiet source.
        logger.error(f"[collect] {req.source} error: {e}", exc_info=True)
        return CollectResponse(
            source=req.source, count=0, items=[], success=False,
            error=str(e),
        )


async def _collect_reddit_purge(req: CollectRequest) -> CollectResponse:
    """Collect Reddit posts, extract and validate ticker symbols."""
    import os
    from app.scraper.collectors.reddit_purge_collector import RedditPurgeCollector

    collector = RedditPurgeCollector()
    ollama_host = req.ollama_host or os.getenv("PRISM_URL", "http://10.0.0.16:7777/agent")
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
            source="reddit", count=0, items=[], success=False,
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
            sp=req.sp,
        )
    else:
        return CollectResponse(
            source="youtube", count=0, items=[], success=False,
            error="Either 'channels' or 'query' is required for youtube collection",
        )

    items = [_serialize_video(v) for v in all_videos]
    return CollectResponse(source="youtube", count=len(items), items=items)


async def _collect_news(req: CollectRequest) -> CollectResponse:
    """Collect news articles from RSS feeds."""
    from app.scraper.collectors.news_collector import NewsCollector, _serialize_article

    collector = NewsCollector()

    # Feed URLs are caller-supplied and reach httpx exactly like /scrape's url,
    # so they get the same scheme/private-address guard. Without it, /collect
    # was an unguarded second door to the internal network.
    try:
        for name, feed in (req.feeds or {}).items():
            check_url(feed, field=f"feeds[{name}]")
        if req.feed_url:
            check_url(req.feed_url, field="feed_url")
    except UnsafeUrlError as exc:
        return CollectResponse(
            source="news", count=0, items=[], success=False, error=str(exc),
        )

    if req.feeds:
        # Multi-feed mode
        articles = await collector.collect_feeds(feeds=req.feeds)
    elif req.feed_url:
        # Single feed mode
        feed_name = req.query or "feed"
        articles = await collector.collect_feed(feed_name, req.feed_url)
    else:
        return CollectResponse(
            source="news", count=0, items=[], success=False,
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
            source="kannapedia", count=0, items=[], success=False,
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
            source="leafly", count=0, items=[], success=False,
            error="query is required for leafly collection",
        )

    collector = LeaflyCollector()
    data = await collector.get_strain(req.query)
    
    if not data:
        return CollectResponse(
            source="leafly", count=0, items=[], success=False,
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
            source="duckduckgo", count=0, items=[], success=False,
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
                    sp=req.sp,
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
