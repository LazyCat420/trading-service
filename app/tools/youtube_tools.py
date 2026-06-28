"""
YouTube maintenance tools for the trading bot agents.

These tools allow the agent to debug and fix broken YouTube scrapers by:
1. Finding the correct handle for a channel name.
2. Testing a handle to see if it has a valid videos tab.
"""

import json
import logging
import sys

from app.tools.registry import registry

logger = logging.getLogger(__name__)




@registry.register(
    name="youtube_test_channel",
    description="Test if a YouTube channel handle is valid and has a videos tab. Returns success or the specific yt-dlp error.",
    parameters={
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The YouTube handle to test, must start with @ (e.g., '@markets').",
            },
        },
        "required": ["handle"],
    },
    tier=0,
    source="local",
)
async def youtube_test_channel(handle: str) -> str:
    """Run yt-dlp to verify a channel handle."""
    import asyncio

    if not handle.startswith("@"):
        handle = f"@{handle}"

    logger.info(f"[YouTubeTools] Testing channel handle: {handle}")

    try:
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            f"https://www.youtube.com/{handle}/videos",
            "--flat-playlist",
            "--dump-json",
            "--playlist-end=1",
            "--no-download",
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode == 0 and stdout_text:
            try:
                # Verify it returned valid JSON representing a video
                video_data = json.loads(stdout_text.split("\n")[0])
                title = video_data.get("title", "Unknown")
                channel = video_data.get("channel", "Unknown")

                return json.dumps(
                    {
                        "status": "success",
                        "handle": handle,
                        "is_valid": True,
                        "channel_name": channel,
                        "latest_video_title": title,
                    }
                )
            except json.JSONDecodeError:
                pass

        # If it failed or wasn't valid JSON
        is_404 = "HTTP Error 404" in stderr_text
        no_videos_tab = "This channel does not have a videos tab" in stderr_text

        return json.dumps(
            {
                "status": "error",
                "handle": handle,
                "is_valid": False,
                "error_summary": "404 Not Found"
                if is_404
                else ("No videos tab" if no_videos_tab else "Unknown error"),
                "full_stderr": stderr_text[:500]
                + ("..." if len(stderr_text) > 500 else ""),
            }
        )

    except asyncio.TimeoutError:
        return json.dumps({"status": "error", "error": "Timeout while testing channel"})
    except Exception as e:
        logger.error(f"[YouTubeTools] Error testing channel: {e}")
        return json.dumps({"status": "error", "error": str(e)})


@registry.register(
    name="youtube_search",
    description="Search YouTube for videos matching a query, or get latest videos from specific channel(s). Returns a list of video objects (video_id, title, channel, duration_secs, url, published_at).",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query (e.g. 'Bloomberg News Live'). Optional if channels is provided.",
            },
            "channels": {
                "type": "array",
                "items": {
                    "type": "string"
                },
                "description": "Optional list of YouTube channel handles or IDs (e.g. ['@ThePrimeagen']) to fetch recent videos from directly.",
            },
            "sort": {
                "type": "string",
                "enum": ["relevance", "date"],
                "description": "Optional sort order for search: 'date' (default) or 'relevance'.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5).",
                "minimum": 1,
                "maximum": 20,
            }
        },
    },
    tier=0,
    source="local",
)
async def youtube_search(query: str = None, channels: list[str] = None, sort: str = "date", limit: int = 5) -> str:
    """Search YouTube or fetch channel videos using scraper-service."""
    import os
    import httpx

    if not query and not channels:
        return json.dumps({"status": "error", "error": "Either 'query' or 'channels' must be provided."})

    scraper_url = os.getenv("SCRAPER_SERVICE_URL", "http://10.0.0.16:8001")

    # Auto-detect "latest" search queries
    is_latest_query = False
    if query:
        q_lower = query.lower()
        if any(w in q_lower for w in ["latest", "recent", "newest", "new"]):
            is_latest_query = True
            if not sort:
                sort = "date"

    logger.info(f"[YouTubeTools] Searching YouTube (query='{query}', channels={channels}, sort='{sort}') via scraper-service at {scraper_url}")

    try:
        payload = {
            "source": "youtube",
            "limit": limit + 5,
            "require_transcript": False,
            "days_back": 0
        }

        if channels:
            payload["channels"] = channels
        else:
            payload["query"] = query
            if sort:
                payload["sort"] = sort

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{scraper_url}/collect", json=payload, timeout=25.0)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                formatted = []

                for item in items:
                    video_id = item.get("video_id")
                    if video_id and len(video_id) == 11:
                        published_at = item.get("published_at")
                        formatted.append({
                            "video_id": video_id,
                            "title": item.get("title"),
                            "channel": item.get("channel"),
                            "duration_secs": item.get("duration_secs"),
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "published_at": published_at
                        })

                # If sorting by date is requested or implied, perform Python-side sort to be absolutely sure
                if sort == "date" or is_latest_query or channels:
                    def get_pub_date(x):
                        p = x.get("published_at")
                        if not p:
                            return ""
                        return p
                    formatted.sort(key=get_pub_date, reverse=True)

                return json.dumps({"status": "success", "results": formatted[:limit]})
            else:
                return json.dumps({"status": "error", "error": f"Scraper service returned status {resp.status_code}: {resp.text}"})
    except Exception as e:
        logger.error(f"[YouTubeTools] Search failed: {e}")
        return json.dumps({"status": "error", "error": str(e)})
