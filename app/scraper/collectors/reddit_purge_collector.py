import asyncio
import logging
import re
import json
import os
from datetime import datetime, timezone
from typing import Any
import yfinance as yf
from fake_useragent import UserAgent

from app.scraper.core.rate_limiter import rate_limiter
from app.scraper.core.session_manager import session_manager
from app.scraper.collectors.reddit_collector import _get_reddit_headers, _parse_rss_entry

logger = logging.getLogger(__name__)

class TickerValidator:
    def __init__(self):
        self.cache: dict[str, bool] = {}
        self.banned_words = {
            "NOT", "FEED", "ON", "FOR", "AND", "OR", "IF", "BUT", "SO", "AT", "BY",
            "TO", "OF", "IN", "IT", "IS", "BE", "AS", "DO", "WE", "UP", "MY", "GO",
            "ME", "US", "THE", "AI", "TLDR", "LOVE", "YOLO", "DD", "ATH", "IMO",
            "USA", "GDP", "CEO", "EOD", "RH", "IRS", "SEC", "WSB", "OP", "EDIT",
            "OUT", "CALL", "PUT", "STRIKE", "EXP", "BUY", "SELL", "HOLD", "MOON",
            "PE", "EPS", "ETF", "NAV", "FD", "IV",
            # Financial-metric / TA acronyms — these are real tickers by coincidence
            # but on stock subreddits they almost always mean the metric, so they
            # flood ticker counts from "fundamentals"/"technicals" discussion
            # threads. Banning them kills the dominant false-positive class.
            "PEG", "SMA", "EMA", "ROE", "ROA", "ROI", "ROIC", "BETA", "EBITDA",
            "EBIT", "EBT", "TTM", "YOY", "QOQ", "YTD", "CAGR", "DCF", "FCF",
            "GAAP", "COGS", "CAPEX", "OPEX", "WACC", "VWAP", "RSI", "MACD", "ATR",
            "ADX", "BVPS", "DPS", "PEGY", "PB", "PS", "DIV", "YLD", "MKT", "VOL",
            "GROSS", "NET", "MARGIN", "REV", "IPO", "FOMC", "CPI", "PPI", "ER",
        }

    def validate_ticker(self, ticker: str) -> bool:
        ticker = ticker.upper().strip()
        if ticker in self.banned_words or len(ticker) < 2 or len(ticker) > 5:
            return False
        
        if ticker in self.cache:
            return self.cache[ticker]

        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1d")
            if hist.empty:
                self.cache[ticker] = False
                return False
            self.cache[ticker] = True
            return True
        except Exception as e:
            logger.debug(f"[reddit-purge] Validation error for {ticker}: {e}")
            self.cache[ticker] = False
            return False


class RedditPurgeCollector:
    def __init__(self):
        self.validator = TickerValidator()

    async def _get_subreddit_posts_rss(self, subreddit: str, listing_type: str, limit: int) -> list[dict]:
        """RSS fallback for when Reddit bot-walls the public .json endpoint (HTTP 403).

        Reddit's .rss feeds stay reachable without OAuth where /*.json now 403s
        from datacenter/residential IPs. RSS carries title/body/author but not
        score/comment counts, so `_parse_rss_entry` supplies neutral defaults; the
        title is what the ticker scorer weights most heavily anyway.
        """
        import feedparser

        url = f"https://www.reddit.com/r/{subreddit}/{listing_type}.rss?limit={limit}"
        domain = "www.reddit.com"
        try:
            async with rate_limiter.acquire(domain):
                r = await session_manager.client.get(url, headers=_get_reddit_headers(), timeout=15.0)
            if r.status_code != 200:
                logger.warning(f"[reddit-purge] RSS fallback failed for r/{subreddit}/{listing_type}: HTTP {r.status_code}")
                return []
            feed = feedparser.parse(r.text)
            posts = []
            for entry in feed.entries[:limit]:
                posts.append(_parse_rss_entry(entry, subreddit))
            logger.info(f"[reddit-purge] RSS fallback returned {len(posts)} posts for r/{subreddit}/{listing_type}")
            return posts
        except Exception as e:
            logger.error(f"[reddit-purge] RSS fallback error for r/{subreddit}: {e}")
            return []

    async def get_subreddit_posts(self, subreddit: str, listing_type: str = "hot", limit: int = 5) -> list[dict]:
        url = f"https://www.reddit.com/r/{subreddit}/{listing_type}.json?limit={limit}"
        domain = "www.reddit.com"
        try:
            async with rate_limiter.acquire(domain):
                r = await session_manager.client.get(url, headers=_get_reddit_headers(), timeout=15.0)
            if r.status_code != 200:
                logger.warning(f"[reddit-purge] Failed to fetch r/{subreddit}/{listing_type}: HTTP {r.status_code} — trying RSS fallback")
                return await self._get_subreddit_posts_rss(subreddit, listing_type, limit)
            
            data = r.json()
            posts = []
            for child in data.get("data", {}).get("children", []):
                post_data = child.get("data", {})
                if post_data:
                    posts.append({
                        "id": post_data.get("id"),
                        "title": post_data.get("title", ""),
                        "subreddit": post_data.get("subreddit", subreddit),
                        "permalink": post_data.get("permalink", ""),
                        "score": post_data.get("score", 0),
                        "selftext": post_data.get("selftext", ""),
                        "num_comments": post_data.get("num_comments", 0),
                        "created_utc": post_data.get("created_utc", 0),
                        "upvote_ratio": post_data.get("upvote_ratio", 0.0),
                        "author": post_data.get("author", "")
                    })
            return posts
        except Exception as e:
            logger.error(f"[reddit-purge] Error fetching posts from r/{subreddit}: {e}")
            return []

    async def get_thread_data(self, permalink: str) -> tuple[str, str, list[str]]:
        url = f"https://www.reddit.com{permalink}.json"
        domain = "www.reddit.com"
        try:
            async with rate_limiter.acquire(domain):
                r = await session_manager.client.get(url, headers=_get_reddit_headers(), timeout=15.0)
            if r.status_code != 200:
                return "", "", []
            
            data = r.json()
            title = ""
            selftext = ""
            comments = []

            if isinstance(data, list) and len(data) > 0:
                post_listing = data[0]
                children = post_listing.get("data", {}).get("children", [])
                if children and children[0].get("kind") == "t3":
                    post_data = children[0].get("data", {})
                    title = post_data.get("title", "")
                    selftext = post_data.get("selftext", "")

            if isinstance(data, list) and len(data) > 1:
                comment_listing = data[1]
                for child in comment_listing.get("data", {}).get("children", [])[:30]:
                    if child.get("kind") == "t1":
                        body = child.get("data", {}).get("body", "")
                        if body and body not in ("[deleted]", "[removed]"):
                            comments.append(body)

            return title, selftext, comments
        except Exception as e:
            logger.warning(f"[reddit-purge] Failed to fetch thread details for {permalink}: {e}")
            return "", "", []

    async def filter_candidates_with_llm(self, candidates: list[dict], ollama_host: str | None = None, ollama_model: str | None = None) -> list[dict]:
        if not candidates:
            return []
        
        # Batch by 20 to avoid massive prompt sizes
        selected = []
        batch_size = 20
        
        prism_url = os.getenv("PRISM_URL", "http://lazy-tool-service:7778/agent")
        base_url = prism_url
        if base_url.endswith("/agent"):
            base_url = base_url[:-6] + "/chat"
        elif "/chat" not in base_url and "/v1" not in base_url:
            if base_url.endswith("/"):
                base_url += "chat"
            else:
                base_url += "/chat"
                
        if "?stream=false" not in base_url:
            base_url += "?stream=false"
        headers = {"Content-Type": "application/json"}
        
        model = ollama_model or os.getenv("PURGE_MODEL", "vllm/cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit")
        provider = "vllm"
        resolved_model = model
        if "/" in model:
            parts = model.split("/", 1)
            provider = parts[0]
            resolved_model = parts[1]

        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i+batch_size]
            titles_text = "\n".join([f"{idx}. {p['title']} (r/{p['subreddit']})" for idx, p in enumerate(batch)])
            prompt = f"""
Review these Reddit thread titles. Identify the indexes of threads that are likely 
discussing a specific stock ticker, earnings play, or catalyst. 
Ignore generic memes, shitposts, or "gain/loss porn" unless a ticker is mentioned.

TITLES:
{titles_text}

Output ONLY a JSON list of indexes: [0, 5, 2]
"""
            payload = {
                "provider": provider,
                "model": resolved_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "maxTokens": 1024,
                "skipConversation": True,
            }
            try:
                r = await session_manager.client.post(base_url, json=payload, headers=headers, timeout=60.0)
                if r.status_code == 200:
                    resp_data = r.json()
                    content = ""
                    if isinstance(resp_data, dict):
                        if "text" in resp_data:
                            content = resp_data["text"]
                        elif "response" in resp_data and isinstance(resp_data["response"], dict):
                            content = resp_data["response"].get("text", "")
                    content = content.strip() if content else ""
                    if "```" in content:
                        content = content.split("```")[1].strip()
                        if content.startswith("json"):
                            content = content[4:].strip()
                    indices = json.loads(content)
                    if isinstance(indices, list):
                        for idx in indices:
                            if 0 <= idx < len(batch):
                                selected.append(batch[idx])
            except Exception as e:
                logger.warning(f"[reddit-purge] Prism filtering failed: {e}")
                # Fallback to including all of them if LLM fails
                return candidates

        return selected

    def extract_tickers(self, text: str) -> list[str]:
        if not text:
            return []
        raw_tickers = re.findall(r'(?:\$|\b)([A-Z]{2,5})\b', text)
        valid = []
        for t in raw_tickers:
            if t.isalpha():
                valid.append(t)
        return list(set(valid))

    async def collect(
        self,
        subreddits: list[str] | None = None,
        use_llm: bool = False,
        ollama_host: str | None = None,
        ollama_model: str | None = None,
        limit: int = 10
    ) -> list[dict]:
        if not subreddits:
            subreddits = ["wallstreetbets", "stocks", "pennystocks", "options"]

        # 1. Hot pinned threads
        candidates = []
        for sub in subreddits:
            posts = await self.get_subreddit_posts(sub, listing_type="hot", limit=3)
            for p in posts:
                if "Daily" in p["title"] or "Moves Tomorrow" in p["title"]:
                    candidates.append(p)
            await asyncio.sleep(1.0)

        # 2. Rising candidates
        for sub in subreddits:
            posts = await self.get_subreddit_posts(sub, listing_type="rising", limit=limit)
            candidates.extend(posts)
            await asyncio.sleep(1.0)

        # De-duplicate candidates by permalink
        unique_candidates = {c["permalink"]: c for c in candidates}.values()

        # 3. LLM Filter
        filtered_candidates = list(unique_candidates)
        if use_llm and ollama_host and ollama_model:
            filtered_candidates = await self.filter_candidates_with_llm(
                filtered_candidates, ollama_host, ollama_model
            )

        # 4. Fetch details & count tickers
        ticker_scores: dict[str, int] = {}
        ticker_to_posts: dict[str, list[dict]] = {}

        for post in filtered_candidates:
            real_title, selftext, comments = await self.get_thread_data(post["permalink"])
            if not real_title:
                real_title = post["title"]
            # Keep any body we already have (e.g. from the RSS fallback) when the
            # thread .json fetch is bot-walled and returns empty.
            if not selftext:
                selftext = post.get("selftext", "")

            # Save fetched texts onto post dict
            post["title"] = real_title
            post["selftext"] = selftext

            # Score ticker mentions
            title_tickers = self.extract_tickers(real_title)
            for t in title_tickers:
                ticker_scores[t] = ticker_scores.get(t, 0) + 3
                ticker_to_posts.setdefault(t, []).append(post)

            body_tickers = self.extract_tickers(selftext)
            for t in body_tickers:
                ticker_scores[t] = ticker_scores.get(t, 0) + 2
                if post not in ticker_to_posts.setdefault(t, []):
                    ticker_to_posts[t].append(post)

            for comment in comments:
                comm_tickers = self.extract_tickers(comment)
                for t in comm_tickers:
                    ticker_scores[t] = ticker_scores.get(t, 0) + 1
                    if post not in ticker_to_posts.setdefault(t, []):
                        ticker_to_posts[t].append(post)

            await asyncio.sleep(1.0)

        # 5. Validate tickers and format response
        results = []
        for ticker, score in ticker_scores.items():
            if self.validator.validate_ticker(ticker):
                results.append({
                    "ticker": ticker,
                    "score": score,
                    "posts": [
                        {
                            "id": p["id"],
                            "title": p["title"],
                            "selftext": p["selftext"],
                            "subreddit": p["subreddit"],
                            "score": p["score"],
                            "num_comments": p["num_comments"],
                            "created_utc": p["created_utc"],
                            "upvote_ratio": p["upvote_ratio"],
                            "author": p["author"],
                            "permalink": p["permalink"],
                            "url": f"https://reddit.com{p['permalink']}"
                        } for p in ticker_to_posts.get(ticker, [])
                    ]
                })

        # Sort by score descending
        results.sort(key=lambda x: x["score"], reverse=True)
        return results
