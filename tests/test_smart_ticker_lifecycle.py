"""
Tests for Smart Ticker Lifecycle features:
  1. Material change detection
  2. Watchlist curator trigger logic
  3. Curator response parsing
"""

import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


# ── Material Change Detection Tests ──────────────────────────────────


class TestMaterialChangeDetection:
    """Tests for _has_material_change() in ticker_selector.py"""

    def _make_mock_db(self, analysis_price=None, analysis_time=None,
                      current_price=None, new_article_count=0):
        """Build a mock DB that returns canned query results."""
        db = MagicMock()
        call_count = [0]

        def mock_execute(query, params=None):
            result = MagicMock()
            q = query.strip()

            if "analysis_results" in q and "price_at_analysis" in q:
                if analysis_price is not None and analysis_time is not None:
                    result.fetchone.return_value = (analysis_price, analysis_time)
                else:
                    result.fetchone.return_value = None
            elif "price_history" in q:
                if current_price is not None:
                    result.fetchone.return_value = (current_price,)
                else:
                    result.fetchone.return_value = None
            elif "news_articles" in q and "COUNT" in q:
                result.fetchone.return_value = (new_article_count,)
            else:
                result.fetchone.return_value = None

            return result

        db.execute = mock_execute
        return db

    def test_no_prior_analysis_returns_false(self):
        """If there's no prior analysis with price data, no comparison possible."""
        from app.pipeline.ticker_selector import _has_material_change

        db = self._make_mock_db(analysis_price=None)
        assert _has_material_change("AAPL", db) is False

    def test_price_move_over_5pct_returns_true(self):
        """A >5% price move since last analysis = material change."""
        from app.pipeline.ticker_selector import _has_material_change

        now = datetime.now(timezone.utc)
        db = self._make_mock_db(
            analysis_price=100.0,
            analysis_time=now - timedelta(hours=12),
            current_price=106.0,  # 6% move
        )
        assert _has_material_change("AAPL", db) is True

    def test_price_move_under_5pct_no_news_returns_false(self):
        """A <5% price move with no new articles = no material change."""
        from app.pipeline.ticker_selector import _has_material_change

        now = datetime.now(timezone.utc)
        db = self._make_mock_db(
            analysis_price=100.0,
            analysis_time=now - timedelta(hours=12),
            current_price=103.0,  # 3% move
            new_article_count=1,
        )
        assert _has_material_change("AAPL", db) is False

    def test_3_plus_new_articles_returns_true(self):
        """3+ new articles since last analysis = material change."""
        from app.pipeline.ticker_selector import _has_material_change

        now = datetime.now(timezone.utc)
        db = self._make_mock_db(
            analysis_price=100.0,
            analysis_time=now - timedelta(hours=12),
            current_price=101.0,  # 1% move (not enough)
            new_article_count=4,  # 4 articles (enough)
        )
        assert _has_material_change("AAPL", db) is True

    def test_price_drop_over_5pct_returns_true(self):
        """A >5% price DROP also counts as material change."""
        from app.pipeline.ticker_selector import _has_material_change

        now = datetime.now(timezone.utc)
        db = self._make_mock_db(
            analysis_price=100.0,
            analysis_time=now - timedelta(hours=12),
            current_price=93.0,  # -7% drop
        )
        assert _has_material_change("AAPL", db) is True

    def test_exception_returns_false(self):
        """If the DB query fails, default to no material change."""
        from app.pipeline.ticker_selector import _has_material_change

        db = MagicMock()
        db.execute.side_effect = Exception("DB error")
        assert _has_material_change("AAPL", db) is False


# ── Curator Trigger Logic Tests ──────────────────────────────────────


class TestCuratorTrigger:
    """Tests for should_trigger_curation() in watchlist_curator.py"""

    def test_empty_decisions_returns_false(self):
        """No decisions = no trigger."""
        from app.cognition.watchlist_curator import should_trigger_curation

        assert should_trigger_curation([]) is False
        assert should_trigger_curation(None) is False

    def test_insufficient_decisions_returns_false(self):
        """Fewer than 3 decisions = no trigger."""
        from app.cognition.watchlist_curator import should_trigger_curation

        now = datetime.now(timezone.utc)
        decisions = [
            {"action": "HOLD", "confidence": 50, "at": now.isoformat()},
            {"action": "HOLD", "confidence": 55, "at": (now - timedelta(days=1)).isoformat()},
        ]
        assert should_trigger_curation(decisions) is False

    def test_3_holds_in_7_days_triggers(self):
        """3 HOLDs within 7 days = trigger."""
        from app.cognition.watchlist_curator import should_trigger_curation

        now = datetime.now(timezone.utc)
        decisions = [
            {"action": "HOLD", "confidence": 50, "at": (now - timedelta(days=6)).isoformat()},
            {"action": "HOLD", "confidence": 55, "at": (now - timedelta(days=3)).isoformat()},
            {"action": "HOLD", "confidence": 48, "at": (now - timedelta(days=1)).isoformat()},
        ]
        assert should_trigger_curation(decisions) is True

    def test_3_sells_in_7_days_triggers(self):
        """3 SELLs within 7 days = trigger."""
        from app.cognition.watchlist_curator import should_trigger_curation

        now = datetime.now(timezone.utc)
        decisions = [
            {"action": "SELL", "confidence": 70, "at": (now - timedelta(days=5)).isoformat()},
            {"action": "SELL", "confidence": 65, "at": (now - timedelta(days=2)).isoformat()},
            {"action": "SELL", "confidence": 72, "at": now.isoformat()},
        ]
        assert should_trigger_curation(decisions) is True

    def test_mixed_hold_sell_triggers(self):
        """Mix of HOLD + SELL within 7 days = trigger."""
        from app.cognition.watchlist_curator import should_trigger_curation

        now = datetime.now(timezone.utc)
        decisions = [
            {"action": "HOLD", "confidence": 50, "at": (now - timedelta(days=4)).isoformat()},
            {"action": "SELL", "confidence": 65, "at": (now - timedelta(days=2)).isoformat()},
            {"action": "HOLD", "confidence": 48, "at": now.isoformat()},
        ]
        assert should_trigger_curation(decisions) is True

    def test_buys_dont_trigger(self):
        """3 BUYs within 7 days = no trigger (only HOLD/SELL count)."""
        from app.cognition.watchlist_curator import should_trigger_curation

        now = datetime.now(timezone.utc)
        decisions = [
            {"action": "BUY", "confidence": 80, "at": (now - timedelta(days=5)).isoformat()},
            {"action": "BUY", "confidence": 75, "at": (now - timedelta(days=2)).isoformat()},
            {"action": "BUY", "confidence": 85, "at": now.isoformat()},
        ]
        assert should_trigger_curation(decisions) is False

    def test_old_decisions_outside_window_dont_trigger(self):
        """3 HOLDs but all older than 7 days = no trigger."""
        from app.cognition.watchlist_curator import should_trigger_curation

        now = datetime.now(timezone.utc)
        decisions = [
            {"action": "HOLD", "confidence": 50, "at": (now - timedelta(days=15)).isoformat()},
            {"action": "HOLD", "confidence": 55, "at": (now - timedelta(days=10)).isoformat()},
            {"action": "HOLD", "confidence": 48, "at": (now - timedelta(days=8)).isoformat()},
        ]
        assert should_trigger_curation(decisions) is False


# ── Curator Response Parsing Tests ────────────────────────────────────


class TestCuratorResponseParsing:
    """Tests for _parse_curator_response() in watchlist_curator.py"""

    def test_valid_json_response(self):
        """Clean JSON response is parsed correctly."""
        from app.cognition.watchlist_curator import _parse_curator_response

        text = '{"decision": "REMOVE", "rationale": "Bad company", "suggested_tier": "glance"}'
        result = _parse_curator_response(text)
        assert result["decision"] == "REMOVE"
        assert result["rationale"] == "Bad company"
        assert result["suggested_tier"] == "glance"

    def test_json_with_surrounding_text(self):
        """JSON embedded in explanatory text is extracted."""
        from app.cognition.watchlist_curator import _parse_curator_response

        text = 'Here is my analysis:\n{"decision": "KEEP", "rationale": "Strong fundamentals", "suggested_tier": "standard"}\nEnd.'
        result = _parse_curator_response(text)
        assert result["decision"] == "KEEP"

    def test_needs_more_data_response(self):
        """NEEDS_MORE_DATA decision is parsed correctly."""
        from app.cognition.watchlist_curator import _parse_curator_response

        text = '{"decision": "NEEDS_MORE_DATA", "rationale": "Missing earnings data", "suggested_tier": "deep"}'
        result = _parse_curator_response(text)
        assert result["decision"] == "NEEDS_MORE_DATA"
        assert result["suggested_tier"] == "deep"

    def test_malformed_json_fallback(self):
        """Malformed JSON falls back to text scanning."""
        from app.cognition.watchlist_curator import _parse_curator_response

        text = "I think this ticker should be REMOVED because the fundamentals are terrible"
        result = _parse_curator_response(text)
        assert result["decision"] == "REMOVE"

    def test_empty_text_defaults_to_keep(self):
        """Empty response defaults to KEEP (safe fallback)."""
        from app.cognition.watchlist_curator import _parse_curator_response

        result = _parse_curator_response("")
        assert result["decision"] == "KEEP"

    def test_invalid_decision_defaults_to_keep(self):
        """Invalid decision value defaults to KEEP."""
        from app.cognition.watchlist_curator import _parse_curator_response

        text = '{"decision": "EXPLODE", "rationale": "test"}'
        result = _parse_curator_response(text)
        assert result["decision"] == "KEEP"
