"""trend_sources — the one authority for trending-mention aggregation.

The same $group-by-ticker query used to live inline five times across
pipeline_service and discovery_mode, each copy hardcoding its collection's
time axis. These tests pin the contract: the field pairing, the min-mentions
floor, the fail-soft error path, and — by source inspection — that neither
call site carries its own copy any more.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pytest

from app.services import trend_sources
from app.services.trend_sources import TREND_SOURCES, trending_mentions

REPO = pathlib.Path(__file__).resolve().parents[2]
SINCE = datetime(2026, 8, 25, tzinfo=timezone.utc)


class TestPipelineShape:
    def _capture(self, monkeypatch, docs=None):
        seen = {}

        def _agg(collection, pipeline):
            seen["collection"], seen["pipeline"] = collection, pipeline
            return docs or []

        monkeypatch.setattr(trend_sources.mongo_store, "aggregate", _agg)
        return seen

    @pytest.mark.parametrize("collection,ts_field", sorted(TREND_SOURCES.items()))
    def test_each_source_matches_on_its_own_time_axis(self, monkeypatch, collection, ts_field):
        seen = self._capture(monkeypatch)
        trending_mentions(collection, SINCE, limit=10)
        match = seen["pipeline"][0]["$match"]
        assert match[ts_field] == {"$gt": SINCE}
        assert match["ticker"] == {"$ne": None}

    def test_min_mentions_floor_present_only_when_asked(self, monkeypatch):
        seen = self._capture(monkeypatch)
        trending_mentions("news_articles", SINCE, limit=15, min_mentions=3)
        stages = [list(st)[0] for st in seen["pipeline"]]
        assert stages == ["$match", "$group", "$match", "$sort", "$limit"]
        assert seen["pipeline"][2] == {"$match": {"mentions": {"$gte": 3}}}

        trending_mentions("news_articles", SINCE, limit=15)
        stages = [list(st)[0] for st in seen["pipeline"]]
        assert stages == ["$match", "$group", "$sort", "$limit"]

    def test_limit_and_sort(self, monkeypatch):
        seen = self._capture(monkeypatch)
        trending_mentions("reddit_posts", SINCE, limit=30)
        assert seen["pipeline"][-1] == {"$limit": 30}
        assert seen["pipeline"][-2] == {"$sort": {"mentions": -1}}

    def test_rows_are_ticker_mention_tuples_and_null_ids_drop(self, monkeypatch):
        self._capture(monkeypatch, docs=[
            {"_id": "CVX", "mentions": 5},
            {"_id": None, "mentions": 9},
            {"_id": "SOFI", "mentions": 3},
        ])
        assert trending_mentions("news_articles", SINCE, limit=10) == [("CVX", 5), ("SOFI", 3)]

    def test_unregistered_collection_refuses(self):
        with pytest.raises(ValueError):
            trending_mentions("price_history", SINCE, limit=5)


class TestFailSoft:
    def test_store_error_returns_empty_and_is_counted(self, monkeypatch):
        calls = {}

        def _boom(collection, pipeline):
            raise RuntimeError("mongo down")

        def _handle(table, context, err):
            calls["args"] = (table, context)

        monkeypatch.setattr(trend_sources.mongo_store, "aggregate", _boom)
        monkeypatch.setattr(trend_sources.mongo_store, "handle_mongo_read_failure", _handle)
        out = trending_mentions("news_articles", SINCE, limit=5, context="discovery news")
        assert out == []
        assert calls["args"] == ("news_articles", "discovery news")


class TestNoInlineCopiesRemain:
    """The consolidation is only real while the copies stay dead."""

    @pytest.mark.parametrize("rel", [
        "app/services/pipeline_service.py",
        "app/services/discovery_mode.py",
    ])
    def test_call_sites_use_the_helper_not_inline_aggregation(self, rel):
        src = (REPO / rel).read_text()
        assert "trending_mentions(" in src
        assert '"$group": {"_id": "$ticker", "mentions"' not in src, (
            f"{rel} regrew an inline copy of the trending aggregation"
        )
