"""
Strategy Tracker Unit Tests — Verify per-prompt P&L tracking and benching.

Tests record_strategy, evaluate_pnl, get_confidence_bonus and
bench_underperformers.

These used to patch `strategy_tracker.get_db` and assert on SQL text
("INSERT INTO strategy_performance" in the executed string). The module calls
`mongo_store`/`mongo_query` now, so the patched `get_db` intercepted nothing
and the assertions were being made against a live database.

The Mongo rewrite also changed WHERE the aggregation happens, and the tests
have to follow. `get_confidence_bonus` and `bench_underperformers` used to read
a win rate that SQL had already computed (`fetchone -> (15, 0.60)`); they now
pull the resolved rows with `find_docs` and count wins in Python. So the
fixtures supply DOCUMENTS with `win` flags, and the win rate is a property of
the data rather than a number handed to the code — a test that fed the rate
directly could no longer fail if the counting broke.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture
def mongo():
    """Patch the module's Mongo layer; dispatch reads on collection name.

    Both the read helper and the write helper are patched. Stubbing only the
    read leaves writes pointed at the real store.
    """
    store = MagicMock()
    query = MagicMock()
    store.find_docs.return_value = []
    query.find_rows.return_value = []
    query.find_row.return_value = None
    with patch("app.trading.strategy_tracker.mongo_store", store), \
         patch("app.trading.strategy_tracker.mongo_query", query):
        yield store, query


def _perf(win: bool, ph: str = "hash123", return_pct: float = 1.0) -> dict:
    """One resolved strategy_performance document."""
    return {
        "agent_prompt_hash": ph,
        "win": win,
        "return_pct": return_pct,
        "resolved_at": "2026-01-01T00:00:00Z",
    }


class TestRecordStrategy:
    """record_strategy should only record BUY/SELL signals."""

    def test_hold_signal_returns_none(self, mongo):
        store, _ = mongo
        from app.trading.strategy_tracker import record_strategy
        result = record_strategy("cand-1", "outcome-1", "hash123", "AAPL", "HOLD", 150.0)
        assert result is None
        store.insert_docs.assert_not_called()

    def test_buy_signal_inserts_record(self, mongo):
        store, _ = mongo
        from app.trading.strategy_tracker import record_strategy
        result = record_strategy("cand-1", "outcome-1", "hash123", "AAPL", "BUY", 150.0)
        assert result is not None
        store.insert_docs.assert_called_once()
        collection, docs = store.insert_docs.call_args[0][:2]
        assert collection == "strategy_performance"
        assert docs[0]["ticker"] == "AAPL"
        assert docs[0]["signal"] == "BUY"
        assert docs[0]["entry_price"] == 150.0

    def test_sell_signal_inserts_record(self, mongo):
        store, _ = mongo
        from app.trading.strategy_tracker import record_strategy
        result = record_strategy("cand-1", "outcome-1", "hash456", "MSFT", "SELL", 400.0)
        assert result is not None
        assert store.insert_docs.call_args[0][1][0]["signal"] == "SELL"

    def test_db_failure_returns_none(self, mongo):
        store, _ = mongo
        store.insert_docs.side_effect = Exception("DB connection lost")
        from app.trading.strategy_tracker import record_strategy
        result = record_strategy("cand-1", "outcome-1", "hash789", "NVDA", "BUY", 800.0)
        assert result is None


class TestEvaluatePnl:
    """evaluate_pnl should resolve open BUY entries for a closed trade."""

    def test_no_open_entries_returns_empty(self, mongo):
        store, query = mongo
        query.find_rows.return_value = []
        from app.trading.strategy_tracker import evaluate_pnl
        result = evaluate_pnl("AAPL", exit_price=165.0)
        assert result == []

    def test_win_trade_resolved_correctly(self, mongo):
        store, query = mongo
        # One open BUY entry: (id, entry_price, signal, agent_prompt_hash)
        query.find_rows.return_value = [("perf-001", 150.0, "BUY", "hash123")]
        from datetime import datetime, timezone
        query.find_row.return_value = (datetime.now(timezone.utc).isoformat(),)

        from app.trading.strategy_tracker import evaluate_pnl
        result = evaluate_pnl("AAPL", exit_price=165.0)

        assert len(result) == 1
        assert result[0]["win"] is True
        assert result[0]["return_pct"] == pytest.approx(10.0, abs=0.1)
        store.update_docs.assert_called()

    def test_loss_trade_resolved_correctly(self, mongo):
        store, query = mongo
        query.find_rows.return_value = [("perf-002", 200.0, "BUY", "hash456")]
        from datetime import datetime, timezone
        query.find_row.return_value = (datetime.now(timezone.utc).isoformat(),)

        from app.trading.strategy_tracker import evaluate_pnl
        result = evaluate_pnl("MSFT", exit_price=180.0)

        assert len(result) == 1
        assert result[0]["win"] is False
        assert result[0]["return_pct"] < 0


class TestGetConfidenceBonus:
    """get_confidence_bonus should return +5 for winning prompts."""

    def test_no_trades_returns_zero(self, mongo):
        store, _ = mongo
        store.find_docs.return_value = []

        with patch("app.trading.strategy_tracker.settings") as mock_settings:
            mock_settings.MIN_TRADES_BEFORE_BENCH = 10
            mock_settings.WIN_RATE_BONUS_THRESHOLD = 0.55
            from app.trading.strategy_tracker import get_confidence_bonus
            result = get_confidence_bonus("hash123")

        assert result == 0

    def test_high_win_rate_returns_bonus(self, mongo):
        store, _ = mongo
        # 15 resolved trades, 9 wins = 60%
        store.find_docs.return_value = [_perf(win=i < 9) for i in range(15)]

        with patch("app.trading.strategy_tracker.settings") as mock_settings:
            mock_settings.MIN_TRADES_BEFORE_BENCH = 10
            mock_settings.WIN_RATE_BONUS_THRESHOLD = 0.55
            from app.trading.strategy_tracker import get_confidence_bonus
            result = get_confidence_bonus("hash123")

        assert result == 5

    def test_low_win_rate_returns_zero(self, mongo):
        store, _ = mongo
        # 20 resolved trades, 7 wins = 35%
        store.find_docs.return_value = [_perf(win=i < 7) for i in range(20)]

        with patch("app.trading.strategy_tracker.settings") as mock_settings:
            mock_settings.MIN_TRADES_BEFORE_BENCH = 10
            mock_settings.WIN_RATE_BONUS_THRESHOLD = 0.55
            from app.trading.strategy_tracker import get_confidence_bonus
            result = get_confidence_bonus("hash123")

        assert result == 0

    def test_below_minimum_trades_returns_zero_even_when_winning(self, mongo):
        """The trade-count floor must gate the bonus, not just the win rate.

        Both thresholds are read from the same settings object; a test that
        only ever supplies enough trades cannot tell whether the floor is
        applied at all.
        """
        store, _ = mongo
        store.find_docs.return_value = [_perf(win=True) for _ in range(3)]

        with patch("app.trading.strategy_tracker.settings") as mock_settings:
            mock_settings.MIN_TRADES_BEFORE_BENCH = 10
            mock_settings.WIN_RATE_BONUS_THRESHOLD = 0.55
            from app.trading.strategy_tracker import get_confidence_bonus
            result = get_confidence_bonus("hash123")

        assert result == 0


class TestBenchUnderperformers:
    """bench_underperformers should deactivate low win rate prompts."""

    def test_no_underperformers_returns_empty(self, mongo):
        store, _ = mongo
        store.find_docs.return_value = []

        with patch("app.trading.strategy_tracker.settings") as mock_settings:
            mock_settings.MIN_TRADES_BEFORE_BENCH = 10
            mock_settings.WIN_RATE_BENCH_THRESHOLD = 0.40
            from app.trading.strategy_tracker import bench_underperformers
            result = bench_underperformers()

        assert result == []

    def test_underperformer_gets_benched(self, mongo):
        store, _ = mongo
        # First call lists active prompts; subsequent calls return that
        # prompt's resolved trades — 15 trades, 4 wins = 27%.
        store.find_docs.side_effect = [
            [{"prompt_hash": "hash_bad", "name": "Bad Strategy", "active": True}],
            [_perf(win=i < 4, ph="hash_bad") for i in range(15)],
        ]

        with patch("app.trading.strategy_tracker.settings") as mock_settings:
            mock_settings.MIN_TRADES_BEFORE_BENCH = 10
            mock_settings.WIN_RATE_BENCH_THRESHOLD = 0.40
            from app.trading.strategy_tracker import bench_underperformers
            result = bench_underperformers()

        assert "hash_bad" in result
        # Verify the prompt was deactivated
        update_calls = [
            c for c in store.update_docs.call_args_list
            if c[0][0] == "generated_agent_prompts"
        ]
        assert len(update_calls) >= 1
        assert update_calls[0][0][2]["$set"]["active"] is False

    def test_good_performer_is_left_active(self, mongo):
        """The bench must not fire on a prompt that clears the threshold.

        Without this, a bench_underperformers that benched everything would
        pass the test above.
        """
        store, _ = mongo
        store.find_docs.side_effect = [
            [{"prompt_hash": "hash_good", "name": "Good Strategy", "active": True}],
            [_perf(win=i < 12, ph="hash_good") for i in range(15)],  # 80%
        ]

        with patch("app.trading.strategy_tracker.settings") as mock_settings:
            mock_settings.MIN_TRADES_BEFORE_BENCH = 10
            mock_settings.WIN_RATE_BENCH_THRESHOLD = 0.40
            from app.trading.strategy_tracker import bench_underperformers
            result = bench_underperformers()

        assert result == []
        store.update_docs.assert_not_called()
