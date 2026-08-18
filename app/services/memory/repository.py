import json
from typing import Any, Dict, List
from app.db import mongo_query, mongo_store


class MemoryRepository:
    """Shared database repository for accessing memory tables."""

    @staticmethod
    def _parse_tags(tags_val: Any) -> list[str]:
        if not tags_val:
            return []
        if isinstance(tags_val, str):
            try:
                return json.loads(tags_val)
            except json.JSONDecodeError:
                return []
        if isinstance(tags_val, list):
            return tags_val
        return []

    @classmethod
    def get_memories_by_ticker(
        cls, ticker: str, active_only: bool = True
    ) -> List[Dict[str, Any]]:
        """Fetches memory rules pertinent to a specific ticker."""
        from app.db.memory_repo import _ensure_schema
        _ensure_schema()
        cols = ["id", "type", "ticker", "sector", "summary", "tags",
                "confidence_score", "evidence_count", "status",
                "last_used_at", "last_validated_at", "created_at", "updated_at"]
        query: dict = {"ticker": ticker}
        if active_only:
            query["status"] = "active"

        memories = []
        for row in mongo_query.find_rows("canonical_memories", query, cols):
            record = dict(zip(cols, row))
            record["tags"] = cls._parse_tags(record.get("tags"))
            memories.append(record)

        return memories

    @classmethod
    def fetch_candidate_memories(
        cls, ticker: str, sector: str | None = None
    ) -> List[Dict[str, Any]]:
        """Fetch active canonical memories for a specific ticker OR its sector."""
        from app.db.memory_repo import _ensure_schema
        _ensure_schema()
        # SELECT * — whole documents, so read dicts rather than a fixed column list.
        query = {
            "status": {"$ne": "deprecated"},
            "$or": [
                {"ticker": ticker},
                {"ticker": None, "sector": sector},
                {"ticker": None, "sector": None},
            ],
        }

        results = []
        for record in mongo_query.find_dicts("canonical_memories", query):
            record.pop("_id", None)
            record["tags"] = cls._parse_tags(record.get("tags"))
            results.append(record)

        return results
