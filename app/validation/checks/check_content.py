"""Candidate Content Check — Synchronous and async existence checks across news, reddit, youtube.

Pure MongoDB implementation.
"""

import asyncio
from app.db import mongo_store


def _sync_check_content(ticker: str) -> bool:
    """
    Synchronous DB check for existing content in MongoDB.
    Returns True if any news, reddit, or youtube content exists for the ticker.
    """
    ticker = ticker.upper().strip()
    if mongo_store.find_docs('news_articles', {'ticker': ticker}, limit=1):
        return True

    if mongo_store.find_docs('reddit_posts', {'ticker': ticker}, limit=1):
        return True

    if mongo_store.find_docs('youtube_transcripts', {'ticker': ticker}, limit=1):
        return True

    return False


async def check_content(ticker: str) -> bool:
    """
    Checks if there is any content for the ticker in the database.
    Returns a boolean indicating if content was found.
    """
    return await asyncio.to_thread(_sync_check_content, ticker)
