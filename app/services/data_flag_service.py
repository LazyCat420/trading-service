"""
Data quality flag service — flag bad data items and manage source trust.

Pure MongoDB implementation for data_flags and source_trust collections.
"""

import logging
import uuid
from datetime import datetime, timezone

from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

# Source table → source field mapping for trust tracking
_SOURCE_FIELD_MAP = {
    "news_articles": ("publisher", "publisher"),
    "reddit_posts": ("subreddit", "subreddit"),
    "youtube_transcripts": ("youtube_channel", "channel"),
}


# ── Flag CRUD ────────────────────────────────────────────────────────


def flag_item(
    source_table: str,
    source_id: str,
    flag_type: str,
    reason: str = "",
    ticker: str = "",
) -> dict:
    """Flag a data item as bad. Updates source trust score in MongoDB."""
    flag_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    valid_tables = {"news_articles", "reddit_posts", "youtube_transcripts"}
    if source_table not in valid_tables:
        raise ValueError(f"source_table must be one of {valid_tables}")

    existing = mongo_query.find_row('data_flags', {'source_table': source_table, 'source_id': source_id}, ['id'])
    if existing:
        return {"flag_id": existing[0], "already_flagged": True}

    mongo_store.insert_docs('data_flags', [{
        'id': flag_id,
        'source_table': source_table,
        'source_id': source_id,
        'ticker': ticker.upper().strip() if ticker else "",
        'flag_type': flag_type,
        'reason': reason,
        'flagged_by': 'user',
        'flagged_at': now,
        'auto_action': 'excluded',
    }])

    trust_info = _update_source_trust(source_table, source_id)

    logger.info(
        "data_flag: flagged %s.%s as %s (reason: %s)",
        source_table,
        source_id,
        flag_type,
        reason[:50],
    )

    return {
        "flag_id": flag_id,
        "source_table": source_table,
        "source_id": source_id,
        "flag_type": flag_type,
        "trust_updated": trust_info,
    }


def unflag_item(flag_id: str) -> bool:
    """Remove a flag. Returns True if flag existed."""
    row = mongo_query.find_row('data_flags', {'id': flag_id}, ['id', 'source_table', 'source_id'])
    if not row:
        return False

    source_table, source_id = row[1], row[2]
    mongo_store.delete_docs('data_flags', {'id': flag_id})

    _update_source_trust(source_table, source_id)

    logger.info("data_flag: unflagged %s", flag_id)
    return True


def get_flags(
    ticker: str | None = None,
    source_table: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List flagged items from MongoDB."""
    query = {}
    if ticker:
        query["ticker"] = ticker.upper().strip()
    if source_table:
        query["source_table"] = source_table

    docs = mongo_store.find_docs(
        "data_flags",
        query,
        sort=[("flagged_at", -1)],
        limit=limit,
    )
    return [
        {
            "id": d.get("id"),
            "source_table": d.get("source_table"),
            "source_id": d.get("source_id"),
            "ticker": d.get("ticker"),
            "flag_type": d.get("flag_type"),
            "reason": d.get("reason"),
            "flagged_at": d.get("flagged_at").isoformat() if hasattr(d.get("flagged_at"), "isoformat") else str(d.get("flagged_at")),
            "auto_action": d.get("auto_action"),
        }
        for d in docs
    ]


def get_flagged_source_ids(source_table: str, ticker: str | None = None) -> set:
    """Get set of source_ids that are flagged for a given table in MongoDB."""
    try:
        query = {"source_table": source_table}
        if ticker:
            query["ticker"] = ticker.upper().strip()
        rows = mongo_query.find_rows('data_flags', query, ['source_id'])
        return {r[0] for r in rows if r and r[0]}
    except Exception:
        return set()


def get_filtered_report(ticker: str) -> dict:
    """Get a transparency report of what was filtered for a ticker from MongoDB."""
    t = ticker.upper().strip()
    report: dict = {"ticker": t, "flagged_items": [], "blocked_sources": []}

    try:
        rows = mongo_query.find_rows(
            'data_flags',
            {'ticker': t},
            ['source_table', 'source_id', 'flag_type', 'reason', 'flagged_at'],
            sort=[('flagged_at', -1)]
        )
        report["flagged_items"] = [
            {
                "source_table": r[0],
                "source_id": r[1],
                "flag_type": r[2],
                "reason": r[3],
                "flagged_at": r[4].isoformat() if hasattr(r[4], "isoformat") else str(r[4]) if r[4] else None,
            }
            for r in rows
        ]
    except Exception:
        pass

    try:
        rows = mongo_query.find_rows(
            'source_trust',
            {'trust_score': {'$lt': 0.5}},
            ['source_type', 'source_name', 'trust_score', 'total_flags', 'total_items', 'flag_rate'],
            sort=[('trust_score', 1)]
        )
        report["blocked_sources"] = [
            {
                "source_type": r[0],
                "source_name": r[1],
                "trust_score": r[2],
                "total_flags": r[3],
                "total_items": r[4],
                "flag_rate": r[5],
            }
            for r in rows
        ]
    except Exception:
        pass

    return report


# ── Source Trust ──────────────────────────────────────────────────────


def get_source_trust(source_type: str, source_name: str) -> float:
    """Get trust score for a source from MongoDB. Returns 1.0 if unknown."""
    try:
        row = mongo_query.find_row('source_trust', {'source_type': source_type, 'source_name': source_name}, ['trust_score'])
        return float(row[0]) if row and row[0] is not None else 1.0
    except Exception:
        return 1.0


def get_untrusted_sources(threshold: float = 0.5) -> list[dict]:
    """Get all sources below trust threshold from MongoDB."""
    try:
        rows = mongo_query.find_rows(
            'source_trust',
            {'trust_score': {'$lt': threshold}},
            ['source_type', 'source_name', 'trust_score', 'total_flags', 'total_items', 'flag_rate'],
            sort=[('trust_score', 1)]
        )
        return [
            {
                "source_type": r[0],
                "source_name": r[1],
                "trust_score": r[2],
                "total_flags": r[3],
                "total_items": r[4],
                "flag_rate": r[5],
            }
            for r in rows
        ]
    except Exception:
        return []


# ── Helpers ──────────────────────────────────────────────────────────


def _update_source_trust(source_table: str, source_id: str) -> dict | None:
    """Recalculate trust score for the source of a flagged item in MongoDB."""
    mapping = _SOURCE_FIELD_MAP.get(source_table)
    if not mapping:
        return None

    source_type, source_column = mapping

    try:
        source_doc = mongo_store.find_docs(source_table, {"id": source_id}, limit=1)
        if not source_doc or not source_doc[0].get(source_column):
            return None
        source_name = source_doc[0][source_column]

        total_items_docs = mongo_store.find_docs(source_table, {source_column: source_name})
        total_items = len(total_items_docs)

        source_ids = {d["id"] for d in total_items_docs if "id" in d}
        total_flags_docs = mongo_store.find_docs(
            "data_flags",
            {"source_table": source_table, "source_id": {"$in": list(source_ids)}}
        )
        total_flags = len(total_flags_docs)

        flag_rate = total_flags / max(total_items, 1)
        trust_score = max(0.0, 1.0 - flag_rate)

        if flag_rate > 0.5:
            trust_score = min(trust_score, 0.3)

        now = datetime.now(timezone.utc)
        mongo_store.update_docs(
            'source_trust',
            {'source_type': source_type, 'source_name': source_name},
            {'$set': {
                'source_type': source_type,
                'source_name': source_name,
                'trust_score': trust_score,
                'total_flags': total_flags,
                'total_items': total_items,
                'flag_rate': flag_rate,
                'last_updated': now,
            }},
            upsert=True
        )

        logger.info(
            "source_trust: %s/%s = %.2f (flags=%d/%d)",
            source_type,
            source_name,
            trust_score,
            total_flags,
            total_items,
        )

        return {
            "source_type": source_type,
            "source_name": source_name,
            "trust_score": trust_score,
            "flag_rate": flag_rate,
        }
    except Exception as e:
        logger.warning("[data_flag_service] _update_source_trust failed: %s", e)
        return None
