"""One authority for "which tickers are trending in <source> since <when>".

Before 2026-08-25 this aggregation was written five times across the
discovery funnel (pipeline_service: news/reddit/youtube; discovery_mode:
news/reddit), each copy hardcoding the collection's time axis inline. A
window or field fix applied to SOME of the copies is how the funnel drifts
(the shared-helper lesson, DK ch.82) — so the copies are gone and both call
sites read this module.

The time axis differs per source (news and youtube stamp `published_at`,
reddit stamps `created_utc`); that pairing lives ONLY in TREND_SOURCES.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.db import mongo_store

logger = logging.getLogger(__name__)

# collection -> the field that carries the document's own time axis
TREND_SOURCES: dict[str, str] = {
    "news_articles": "published_at",
    "reddit_posts": "created_utc",
    "youtube_transcripts": "published_at",
}


def trending_mentions(
    collection: str,
    since: datetime,
    *,
    limit: int,
    min_mentions: int = 0,
    context: str = "trending",
) -> list[tuple[str, int]]:
    """Tickers by mention count in `collection` since `since`, descending.

    Returns [] on any store error, after routing it through
    `handle_mongo_read_failure` so a dead read is counted, not swallowed.
    `min_mentions` adds a floor (discovery_mode's "3+ mentions" rule);
    0 keeps every ticker seen at least once.
    """
    ts_field = TREND_SOURCES.get(collection)
    if ts_field is None:
        raise ValueError(f"{collection!r} is not a registered trend source")
    pipeline: list[dict] = [
        {"$match": {"ticker": {"$ne": None}, ts_field: {"$gt": since}}},
        {"$group": {"_id": "$ticker", "mentions": {"$sum": 1}}},
    ]
    if min_mentions > 0:
        pipeline.append({"$match": {"mentions": {"$gte": min_mentions}}})
    pipeline.extend([
        {"$sort": {"mentions": -1}},
        {"$limit": int(limit)},
    ])
    try:
        return [
            (d["_id"], d.get("mentions", 0))
            for d in mongo_store.aggregate(collection, pipeline)
            if d.get("_id")
        ]
    except Exception as e:  # noqa: BLE001 — a dead source must not kill discovery
        mongo_store.handle_mongo_read_failure(collection, context, e)
        return []
