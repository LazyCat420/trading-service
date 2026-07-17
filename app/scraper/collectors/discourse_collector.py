"""
discourse_collector.py — Discourse forum collector
-----------------------------------------------------
Collects posts from Discourse-based forums using their public JSON API.
No scraping needed — Discourse exposes /latest.json, /t/{id}.json, etc.

Primary target: Overgrow.com (cannabis growing community)
Works with ANY Discourse forum by changing base_url.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.scraper.core.rate_limiter import rate_limiter
from app.scraper.core.session_manager import session_manager

logger = logging.getLogger(__name__)


@dataclass
class ForumPost:
    """Normalized forum post data."""
    id: str
    topic_id: int
    title: str
    body: str
    author: str
    created_at: datetime | None
    url: str
    forum_name: str
    category: str
    tags: list[str]
    post_number: int = 1
    reply_count: int = 0
    like_count: int = 0
    views: int = 0
    image_urls: list[str] = field(default_factory=list)


class DiscourseCollector:
    """Collects posts from Discourse forums via public JSON API.

    Discourse API endpoints used:
      - /latest.json — Latest topics
      - /top.json — Top topics (by period)
      - /c/{category_slug}/{id}.json — Topics in category
      - /t/{topic_id}.json — Full topic with posts
      - /search.json — Full-text search
      - /tags/{tag}.json — Topics by tag

    No API key required for public forums.
    """

    def __init__(self, base_url: str, forum_name: str = "discourse"):
        self.base_url = base_url.rstrip("/")
        self.forum_name = forum_name

    async def get_latest_topics(
        self,
        limit: int = 30,
        page: int = 0,
    ) -> list[ForumPost]:
        """Get latest topics from the forum."""
        url = f"{self.base_url}/latest.json"
        params = {"page": page}

        data = await self._api_get(url, params)
        if not data:
            return []

        topics = data.get("topic_list", {}).get("topics", [])
        users = {u["id"]: u for u in data.get("users", [])}

        results = []
        for topic in topics[:limit]:
            post = self._topic_to_post(topic, users)
            if post:
                results.append(post)

        logger.info(f"[discourse] {self.forum_name}: {len(results)} latest topics")
        return results

    async def get_top_topics(
        self,
        period: str = "weekly",
        limit: int = 30,
    ) -> list[ForumPost]:
        """Get top topics by period: daily, weekly, monthly, quarterly, yearly, all."""
        url = f"{self.base_url}/top.json"
        params = {"period": period}

        data = await self._api_get(url, params)
        if not data:
            return []

        topics = data.get("topic_list", {}).get("topics", [])
        users = {u["id"]: u for u in data.get("users", [])}

        results = []
        for topic in topics[:limit]:
            post = self._topic_to_post(topic, users)
            if post:
                results.append(post)

        logger.info(f"[discourse] {self.forum_name}: {len(results)} top/{period} topics")
        return results

    async def get_category_topics(
        self,
        category_slug: str,
        category_id: int,
        limit: int = 30,
    ) -> list[ForumPost]:
        """Get topics from a specific category."""
        url = f"{self.base_url}/c/{category_slug}/{category_id}.json"

        data = await self._api_get(url)
        if not data:
            return []

        topics = data.get("topic_list", {}).get("topics", [])
        users = {u["id"]: u for u in data.get("users", [])}

        results = []
        for topic in topics[:limit]:
            post = self._topic_to_post(topic, users)
            if post:
                results.append(post)

        logger.info(f"[discourse] {self.forum_name}/{category_slug}: {len(results)} topics")
        return results

    async def get_topic_posts(
        self,
        topic_id: int,
        max_posts: int = 50,
    ) -> list[ForumPost]:
        """Get all posts in a specific topic (thread)."""
        url = f"{self.base_url}/t/{topic_id}.json"

        data = await self._api_get(url)
        if not data:
            return []

        title = data.get("title", "")
        category_id = data.get("category_id", 0)
        tags = data.get("tags", [])

        post_stream = data.get("post_stream", {})
        posts = post_stream.get("posts", [])

        results = []
        for post_data in posts[:max_posts]:
            created = post_data.get("created_at", "")
            created_at = None
            if created:
                try:
                    created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except Exception:
                    pass

            # Extract images from cooked HTML before stripping
            cooked_html = post_data.get("cooked", "")
            image_urls = self._extract_images(cooked_html)

            # Strip HTML from cooked content
            body = self._strip_html(cooked_html)

            if not body or len(body) < 10:
                continue

            results.append(ForumPost(
                id=str(post_data.get("id", "")),
                topic_id=topic_id,
                title=title,
                body=body,
                author=post_data.get("username", ""),
                created_at=created_at,
                url=f"{self.base_url}/t/{data.get('slug', '')}/{topic_id}/{post_data.get('post_number', 1)}",
                forum_name=self.forum_name,
                category=str(category_id),
                tags=tags,
                post_number=post_data.get("post_number", 1),
                reply_count=post_data.get("reply_count", 0),
                like_count=post_data.get("actions_summary", [{}])[0].get("count", 0) if post_data.get("actions_summary") else 0,
                image_urls=image_urls,
            ))

        logger.info(f"[discourse] Topic {topic_id}: {len(results)} posts")
        return results

    async def search(
        self,
        query: str,
        category_slug: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[ForumPost]:
        """Search the forum, trying DuckDuckGo site search first, then falling back to internal search."""
        from urllib.parse import urlparse
        domain = urlparse(self.base_url).netloc

        results = []
        ddg_success = False

        try:
            from ddgs import DDGS
            ddg_query = f"site:{domain} {query}"
            logger.info(f"[discourse] Querying DuckDuckGo: {ddg_query}")

            def run_ddg():
                with DDGS() as ddgs:
                    return list(ddgs.text(ddg_query, max_results=20))

            loop = asyncio.get_running_loop()
            ddg_results = await loop.run_in_executor(None, run_ddg)

            if ddg_results:
                import re
                topic_ids = []
                for item in ddg_results:
                    href = item.get("href", "")
                    match = re.search(r"/t/(?:[^/]+/)?(\d+)", href)
                    if match:
                        tid = int(match.group(1))
                        if tid not in topic_ids:
                            topic_ids.append(tid)

                logger.info(f"[discourse] DDG search found topic IDs: {topic_ids}")

                # Fetch posts from the top topics found (limit to top 5 to avoid rate limits)
                for topic_id in topic_ids[:5]:
                    try:
                        topic_posts = await self.get_topic_posts(topic_id, max_posts=15)
                        results.extend(topic_posts)
                    except Exception as e:
                        logger.error(f"[discourse] Failed to fetch posts for topic {topic_id}: {e}")

                if results:
                    ddg_success = True
                    logger.info(f"[discourse] DDG search yielded {len(results)} posts")
        except Exception as e:
            logger.warning(f"[discourse] DDG search failed: {e}. Falling back to internal search.")

        if not ddg_success:
            logger.info(f"[discourse] Falling back to internal search for '{query}'")
            search_query = query
            if category_slug:
                search_query += f" category:{category_slug}"
            if tags:
                search_query += f" tags:{','.join(tags)}"

            url = f"{self.base_url}/search.json"
            params = {"q": search_query}

            data = await self._api_get(url, params)
            if not data:
                return []

            topics = data.get("topics", [])
            posts_data = data.get("posts", [])

            # Build topic title lookup
            topic_titles = {t["id"]: t for t in topics}

            for post_data in posts_data[:limit]:
                topic_id = post_data.get("topic_id", 0)
                topic_info = topic_titles.get(topic_id, {})

                created = post_data.get("created_at", "")
                created_at = None
                if created:
                    try:
                        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    except Exception:
                        pass

                cooked_html = post_data.get("cooked", "")
                image_urls = self._extract_images(cooked_html)
                body = self._strip_html(cooked_html) or post_data.get("blurb", "")

                if not body or len(body) < 10:
                    continue

                results.append(ForumPost(
                    id=str(post_data.get("id", "")),
                    topic_id=topic_id,
                    title=topic_info.get("title", ""),
                    body=body,
                    author=post_data.get("username", ""),
                    created_at=created_at,
                    url=f"{self.base_url}/t/{topic_info.get('slug', '')}/{topic_id}/{post_data.get('post_number', 1)}",
                    forum_name=self.forum_name,
                    category=str(topic_info.get("category_id", "")),
                    tags=topic_info.get("tags", []),
                    post_number=post_data.get("post_number", 1),
                    like_count=post_data.get("like_count", 0),
                    image_urls=image_urls,
                ))

            logger.info(f"[discourse] Internal search '{query}': {len(results)} results")

        # Apply limit to total results
        return results[:limit]

    async def get_topics_by_tag(
        self,
        tag: str,
        limit: int = 30,
    ) -> list[ForumPost]:
        """Get topics tagged with a specific tag."""
        url = f"{self.base_url}/tag/{tag}.json"

        data = await self._api_get(url)
        if not data:
            return []

        topics = data.get("topic_list", {}).get("topics", [])
        users = {u["id"]: u for u in data.get("users", [])}

        results = []
        for topic in topics[:limit]:
            post = self._topic_to_post(topic, users)
            if post:
                results.append(post)

        logger.info(f"[discourse] Tag '{tag}': {len(results)} topics")
        return results

    async def _api_get(self, url: str, params: dict | None = None) -> dict | None:
        """Make rate-limited GET request to Discourse API."""
        from urllib.parse import urlparse
        domain = urlparse(self.base_url).netloc

        try:
            async with rate_limiter.acquire(domain):
                r = await session_manager.client.get(
                    url,
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=30.0,
                )

            if r.status_code == 429:
                logger.warning(f"[discourse] {self.forum_name}: rate limited")
                return None

            if r.status_code != 200:
                logger.warning(f"[discourse] {self.forum_name}: HTTP {r.status_code} for {url}")
                return None

            return r.json()

        except Exception as e:
            logger.error(f"[discourse] {self.forum_name} API error: {e}")
            return None

    def _topic_to_post(self, topic: dict, users: dict) -> ForumPost | None:
        """Convert a Discourse topic dict to ForumPost."""
        title = topic.get("title", "")
        if not title:
            return None

        # Find the original poster
        posters = topic.get("posters", [])
        op_user_id = None
        for p in posters:
            if "Original Poster" in p.get("description", ""):
                op_user_id = p.get("user_id")
                break
        author = users.get(op_user_id, {}).get("username", "") if op_user_id else ""

        created = topic.get("created_at", "")
        created_at = None
        if created:
            try:
                created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                pass

        return ForumPost(
            id=str(topic.get("id", "")),
            topic_id=topic.get("id", 0),
            title=title,
            body=topic.get("excerpt", ""),
            author=author,
            created_at=created_at,
            url=f"{self.base_url}/t/{topic.get('slug', '')}/{topic.get('id', '')}",
            forum_name=self.forum_name,
            category=str(topic.get("category_id", "")),
            tags=topic.get("tags", []),
            reply_count=topic.get("reply_count", 0),
            like_count=topic.get("like_count", 0),
            views=topic.get("views", 0),
        )

    def _extract_images(self, html: str) -> list[str]:
        """Extract image URLs from post HTML, ignoring emoticons/smileys/avatars."""
        import re
        from bs4 import BeautifulSoup
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "lxml")
            images = []
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-orig-src")
                if not src:
                    continue
                # Skip emoticons, avatars, small icons
                src_lower = src.lower()
                if any(term in src_lower for term in [
                    "emoji", "emoticon", "avatar", "smiley", "icon", 
                    "profile", "logo", "flag", "badge", "gravatar",
                    "/images/emoji/", "/plugins/discourse-"
                ]):
                    continue
                # Skip very small images (if size is indicated in attributes)
                width = img.get("width")
                height = img.get("height")
                try:
                    if width and int(width) < 50:
                        continue
                    if height and int(height) < 50:
                        continue
                except ValueError:
                    pass
                
                # Make URL absolute if relative
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = self.base_url + src
                
                if src not in images:
                    images.append(src)
            return images
        except Exception as e:
            logger.error(f"[discourse] Failed to extract images: {e}")
            return []

    @staticmethod
    def _strip_html(html: str) -> str:
        """Remove HTML tags from Discourse cooked content."""
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text


def _serialize_forum_post(post: ForumPost) -> dict:
    """Convert ForumPost to JSON-safe dict for API responses."""
    return {
        "id": post.id,
        "topic_id": post.topic_id,
        "title": post.title,
        "body": post.body,
        "author": post.author,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "url": post.url,
        "forum_name": post.forum_name,
        "category": post.category,
        "tags": post.tags,
        "post_number": post.post_number,
        "reply_count": post.reply_count,
        "like_count": post.like_count,
        "views": post.views,
        "image_urls": post.image_urls,
    }
