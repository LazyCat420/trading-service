"""
stream.py — yt-dlp video stream URL extraction endpoint
---------------------------------------------------------
Extracts direct CDN URLs for YouTube videos using yt-dlp.
Returns the URL for use in HTML5 <video> elements, bypassing
YouTube embed restrictions (including age-gating).

No actual video bytes are proxied — the browser streams
directly from YouTube's CDN using the extracted URL.
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# Simple in-memory cache: video_id -> { url, audio_url, expires_at }
_url_cache: dict[str, dict] = {}
_CACHE_TTL_SECS = 3600  # YouTube CDN URLs typically expire in ~6 hours; cache 1 hour


class StreamResponse(BaseModel):
    """Response from the /stream endpoint."""
    video_id: str
    url: str
    audio_url: str | None = None
    format: str = "mp4"
    width: int | None = None
    height: int | None = None
    expires_at: str | None = None
    cached: bool = False


def _extract_expiry_from_url(url: str) -> str | None:
    """Try to extract the 'expire' param from YouTube CDN URLs."""
    match = re.search(r"[?&]expire=(\d+)", url)
    if match:
        ts = int(match.group(1))
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return None


def _get_cached(video_id: str) -> dict | None:
    """Return cached URL if still valid."""
    entry = _url_cache.get(video_id)
    if not entry:
        return None
    if entry.get("cached_at", 0) + _CACHE_TTL_SECS < datetime.now(timezone.utc).timestamp():
        del _url_cache[video_id]
        return None
    return entry


def _set_cache(video_id: str, data: dict) -> None:
    """Cache the extracted URL data."""
    data["cached_at"] = datetime.now(timezone.utc).timestamp()
    _url_cache[video_id] = data
    # Evict old entries if cache grows too large
    if len(_url_cache) > 200:
        oldest_key = min(_url_cache, key=lambda k: _url_cache[k].get("cached_at", 0))
        del _url_cache[oldest_key]


async def _extract_stream_url(video_id: str) -> dict:
    """Use yt-dlp to extract the direct stream URL for a YouTube video."""
    url = f"https://www.youtube.com/watch?v={video_id}"

    cookies_file = os.environ.get("YOUTUBE_COOKIES_FILE", "")
    has_cookies = False
    if cookies_file and os.path.exists(cookies_file):
        # Only use cookies if the file is non-empty and looks like a valid
        # Netscape format cookies file. An empty or malformed file causes
        # yt-dlp to error out immediately on every request.
        try:
            size = os.path.getsize(cookies_file)
            if size > 0:
                with open(cookies_file, "r") as f:
                    first_line = f.readline().strip()
                if first_line.startswith("# Netscape HTTP Cookie File") or first_line.startswith("# HTTP Cookie File"):
                    has_cookies = True
                else:
                    logger.warning(f"[stream] Cookies file exists but is not Netscape format (first line: {first_line[:60]}), ignoring")
            else:
                logger.warning("[stream] Cookies file exists but is empty, ignoring")
        except Exception as e:
            logger.warning(f"[stream] Error reading cookies file: {e}, ignoring")

    # Strategy: try combined mp4 first (plays in <video> without JS),
    # then fall back to best available format
    format_specs = [
        # 1. Best combined mp4 up to 720p (single URL, plays anywhere)
        "best[ext=mp4][height<=720]",
        # 2. Best combined format at any quality
        "best[ext=mp4]",
        # 3. Any best combined format
        "best",
    ]

    for fmt in format_specs:
        cmd = [
            sys.executable, "-m", "yt_dlp",
            url,
            "-f", fmt,
            "--get-url",
            "--no-download",
            "--no-playlist",
            "--no-warnings",
            "--socket-timeout", "15",
        ]

        if has_cookies:
            cmd.extend(["--cookies", cookies_file])

        # Also get format info via --dump-json for metadata
        cmd_json = [
            sys.executable, "-m", "yt_dlp",
            url,
            "-f", fmt,
            "--dump-json",
            "--no-download",
            "--no-playlist",
            "--no-warnings",
            "--socket-timeout", "15",
        ]

        if has_cookies:
            cmd_json.extend(["--cookies", cookies_file])

        try:
            # Get the direct URL
            result = await asyncio.to_thread(
                _run_ytdlp_cmd, cmd, timeout=45
            )

            if not result or not result.strip():
                continue

            stream_url = result.strip().split("\n")[0]  # First URL

            # Try to get metadata too (non-blocking, best-effort)
            width, height = None, None
            try:
                meta_result = await asyncio.to_thread(
                    _run_ytdlp_cmd, cmd_json, timeout=45
                )
                if meta_result:
                    meta = json.loads(meta_result)
                    width = meta.get("width")
                    height = meta.get("height")
            except Exception:
                pass  # Metadata is optional

            expires_at = _extract_expiry_from_url(stream_url)

            return {
                "video_id": video_id,
                "url": stream_url,
                "audio_url": None,
                "format": "mp4",
                "width": width,
                "height": height,
                "expires_at": expires_at,
            }

        except Exception as e:
            logger.warning(f"[stream] Format '{fmt}' failed for {video_id}: {e}")
            continue

    # If all format specs failed, raise
    raise ValueError(f"Could not extract stream URL for {video_id}")


def _run_ytdlp_cmd(cmd: list[str], timeout: int = 45) -> str | None:
    """Run a yt-dlp subprocess and return stdout."""
    import subprocess

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode != 0:
            stderr = result.stderr[:300] if result.stderr else ""
            logger.warning(f"[stream] yt-dlp exited {result.returncode}: {stderr}")
            return None

        return result.stdout

    except subprocess.TimeoutExpired:
        logger.error(f"[stream] yt-dlp timed out after {timeout}s")
        return None


@router.get("/stream/{video_id}")
async def get_stream_url(video_id: str) -> StreamResponse:
    """Extract direct CDN stream URL for a YouTube video.

    Returns a URL that can be used in an HTML5 <video> element.
    The URL typically expires after ~6 hours.
    """
    # Validate video ID format (11 alphanumeric chars + dashes/underscores)
    if not re.match(r"^[a-zA-Z0-9_-]{11}$", video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube video ID format")

    # Check cache first
    cached = _get_cached(video_id)
    if cached:
        logger.info(f"[stream] Cache hit for {video_id}")
        return StreamResponse(**cached, cached=True)

    # Extract fresh URL
    try:
        logger.info(f"[stream] Extracting stream URL for {video_id}...")
        data = await _extract_stream_url(video_id)
        _set_cache(video_id, data)
        logger.info(f"[stream] Successfully extracted URL for {video_id} ({data.get('width')}x{data.get('height')})")
        return StreamResponse(**data, cached=False)

    except ValueError as e:
        logger.error(f"[stream] Extraction failed for {video_id}: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Could not extract stream URL. Video may be private, deleted, or require authentication. Error: {e}"
        )
    except Exception as e:
        logger.error(f"[stream] Unexpected error for {video_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error extracting stream URL")
