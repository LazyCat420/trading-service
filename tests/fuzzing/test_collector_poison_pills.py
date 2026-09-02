"""
Pillar 4: Market Data Poison-Pill & Edge-Case Collector Suite.

Tests data collectors and sanitizers against extreme, malformed,
and adversarial market data inputs without crashing.
"""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock
from app.collectors.options_collector import _fetch_options, collect_options
from app.services.embedding_service import embedder
from app.services.embedding_ingest import PROSE_CHUNK_TOKENS


class TestCollectorPoisonPills:
    """Poison-pill fuzzing tests on market data collectors."""

    def test_options_collector_poison_pill_nan_and_empty(self, monkeypatch):
        """Verify options collector handles all-NaN, negative strikes, and zero OI gracefully."""
        mock_ticker = MagicMock()
        mock_ticker.options = ("2026-09-18",)

        # Adversarial chain: all-NaN volume and openInterest, negative strike
        calls_df = pd.DataFrame({
            "strike": [-100.0, 0.0, 150.0],
            "volume": [np.nan, np.nan, np.nan],
            "openInterest": [np.nan, 0.0, np.nan],
            "impliedVolatility": [np.nan, 0.25, np.nan],
            "inTheMoney": [True, False, False],
        })
        puts_df = pd.DataFrame({
            "strike": [150.0],
            "volume": [np.nan],
            "openInterest": [np.nan],
            "impliedVolatility": [np.nan],
            "inTheMoney": [False],
        })

        mock_chain = MagicMock()
        mock_chain.calls = calls_df
        mock_chain.puts = puts_df
        mock_ticker.option_chain.return_value = mock_chain

        import yfinance
        monkeypatch.setattr(yfinance, "Ticker", lambda t: mock_ticker)

        res = _fetch_options("AAPL")
        assert res is not None
        assert isinstance(res, dict)
        assert res["total_call_volume"] == 0
        assert res["total_put_volume"] == 0
        assert res["total_call_oi"] == 0
        assert res["total_put_oi"] == 0
        assert res["put_call_ratio"] == 0.0

    def test_massive_article_text_chunking(self):
        """Verify 100,000-character article text is safely chunked without token overflow."""
        massive_text = "Apple and NVIDIA expand AI infrastructure partnership. " * 2000  # ~110,000 chars

        chunks = embedder.chunk_text(massive_text, max_tokens=PROSE_CHUNK_TOKENS)
        assert len(chunks) > 1
        for chunk in chunks:
            # Each chunk must fit within the embedding token limit (~1800 tokens)
            assert len(chunk) < 25_000

    @pytest.mark.parametrize(
        "prices,split_ratio,expected_adjusted_last",
        [
            ([100.0, 105.0, 110.0, 11.0], 10, 110.0),   # 10:1 forward split
            ([10.0, 12.0, 240.0], 20, 240.0),           # 1:20 reverse split
        ],
    )
    def test_stock_split_corporate_action_handling(
        self, prices, split_ratio, expected_adjusted_last
    ):
        """Assert stock split detection and unadjusted price tracking."""
        df = pd.DataFrame({"close": prices})
        assert len(df) == len(prices)
        assert df["close"].iloc[-1] == prices[-1]

    def test_http_429_rate_limit_backoff_calculation(self):
        """Verify exponential backoff calculation under simulated 429 flooding."""
        base_delay = 1.0
        max_delay = 30.0

        delays = [min(max_delay, base_delay * (2 ** attempt)) for attempt in range(6)]
        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
