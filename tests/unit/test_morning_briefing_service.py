"""Unit tests for Morning Briefing generator service and schema contracts."""

from datetime import datetime, timedelta, timezone
import pytest

from app.services import morning_briefing


class TestMorningBriefingNextId:
    def test_continues_morning_briefings_sequence(self, monkeypatch):
        monkeypatch.setattr(morning_briefing.mongo_query, "agg_row",
                            lambda *a, **k: (15,))
        assert morning_briefing._next_morning_briefing_id() == 16

    def test_first_ever_morning_briefing_starts_at_one(self, monkeypatch):
        monkeypatch.setattr(morning_briefing.mongo_query, "agg_row",
                            lambda *a, **k: (None,))
        assert morning_briefing._next_morning_briefing_id() == 1

    def test_failed_max_query_falls_back_gracefully(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("mongo failure")
        monkeypatch.setattr(morning_briefing.mongo_query, "agg_row", boom)
        assert isinstance(morning_briefing._next_morning_briefing_id(), int)


class TestMorningBriefingDocStructure:
    def test_stamps_all_required_columns(self, monkeypatch):
        monkeypatch.setattr(morning_briefing.mongo_query, "agg_row",
                            lambda *a, **k: (10,))
        doc = morning_briefing._morning_briefing_doc("Test report content", ["NVDA", "AAPL"])

        assert doc["id"] == 11
        assert isinstance(doc["created_at"], datetime)
        assert doc["report_content"] == "Test report content"
        assert doc["tickers_evaluated"] == ["NVDA", "AAPL"]

    def test_created_at_is_utc(self, monkeypatch):
        monkeypatch.setattr(morning_briefing.mongo_query, "agg_row",
                            lambda *a, **k: (1,))
        stamped = morning_briefing._morning_briefing_doc("x", [])["created_at"]
        assert stamped.tzinfo is not None
        assert abs(stamped - datetime.now(timezone.utc)) < timedelta(seconds=5)


class TestGetRecentMorningBriefings:
    def test_normalises_array_columns_and_dates(self, monkeypatch):
        rows = [
            (10, datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc), "Morning 1", '["NVDA","AAPL"]'),
            (9, datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc), "Morning 2", ["MSFT", "GOOGL"]),
            (8, datetime(2026, 8, 22, 13, 0, tzinfo=timezone.utc), "Morning 3", None),
        ]
        monkeypatch.setattr(morning_briefing.mongo_query, "find_rows", lambda *a, **k: rows)
        res = morning_briefing.get_recent_morning_briefings(limit=5)
        assert len(res) == 3
        assert res[0]["id"] == 10
        assert res[0]["tickers_evaluated"] == ["NVDA", "AAPL"]
        assert res[1]["tickers_evaluated"] == ["MSFT", "GOOGL"]
        assert res[2]["tickers_evaluated"] == []
