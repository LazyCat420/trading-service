"""
Unit tests for smart-money return/alpha computation.

These guard the two properties that make the whole feature trustworthy:
  1. A sell is scored inverted (dodging a drop is skill, not failure).
  2. Nothing is ever fabricated — a missing price or missing benchmark yields
     None, never 0 and never a raw return masquerading as alpha.

The fabricated formulas these replaced (`8.0 + (buys-sells)*0.5` for congress,
a 0.15 baseline + hardcoded alpha bonus for funds) are exactly the kind of thing
that looks fine in a chart and is completely meaningless, so the no-fabrication
assertions below are the point of this file.

Unit tests only — no DB connection needed.
"""

import pytest

from app.analytics.amount_parser import (
    parse_amount_range,
    CONFIDENCE_RANGE,
    CONFIDENCE_BOUND,
    CONFIDENCE_NONE,
)
from app.analytics.returns_engine import _pct, _score_rows, WINDOWS


class TestAmountParser:
    """Congress discloses brackets in at least three formats, all seen live."""

    def test_standard_dollar_range_uses_midpoint(self):
        value, conf = parse_amount_range("$1,001 - $15,000")
        assert value == pytest.approx(8000.5)
        assert conf == CONFIDENCE_RANGE

    def test_abbreviated_k_format(self):
        value, conf = parse_amount_range("15K-50K")
        assert value == pytest.approx(32500.0)
        assert conf == CONFIDENCE_RANGE

    def test_millions_range(self):
        value, conf = parse_amount_range("$1,000,001 - $5,000,000")
        assert value == pytest.approx(3000000.5)
        assert conf == CONFIDENCE_RANGE

    def test_open_ended_bound_is_not_inflated(self):
        # Only a lower bound is known. Understating a whale trade is safer than
        # inventing a ceiling for it.
        value, conf = parse_amount_range("$15,001")
        assert value == pytest.approx(15001.0)
        assert conf == CONFIDENCE_BOUND

    @pytest.mark.parametrize("raw", ["", None, "N/A", "   "])
    def test_unparseable_returns_none_not_zero(self, raw):
        # Zero would render as a real $0 trade in a chart.
        value, conf = parse_amount_range(raw)
        assert value is None
        assert conf == CONFIDENCE_NONE


class TestPct:
    def test_basic_percentage_change(self):
        assert _pct(110.0, 100.0) == pytest.approx(10.0)

    def test_negative_change(self):
        assert _pct(90.0, 100.0) == pytest.approx(-10.0)

    @pytest.mark.parametrize("new,old", [(None, 100.0), (110.0, None), (110.0, 0)])
    def test_missing_or_zero_base_returns_none(self, new, old):
        assert _pct(new, old) is None


def _row(direction, entry, fwd, bench_entry, bench_fwd, size_raw=None, size_est=None):
    """Build one scoring-query row.

    Column order must match _build_scoring_query:
      trade_key, actor_id, actor_name, ticker, direction, event_date,
      size_est_usd, size_confidence, size_raw, entry_price,
      *forward_prices, bench_entry, *bench_forward_prices
    """
    n = len(WINDOWS)
    return (
        "key1", "A1", "Actor One", "TEST", direction, "2025-01-01",
        size_est, None, size_raw, entry,
        *([fwd] * n),
        bench_entry,
        *([bench_fwd] * n),
    )


class TestScoring:
    def test_buy_that_beats_benchmark_has_positive_alpha(self):
        # Stock +20%, SPY +5% → alpha +15.
        rows = [_row("buy", 100.0, 120.0, 100.0, 105.0)]
        scored = _score_rows(rows, "congress")
        assert len(scored) == 1
        alphas = scored[0][-len(WINDOWS):]
        assert all(a == pytest.approx(15.0) for a in alphas)

    def test_buy_that_lags_benchmark_has_negative_alpha(self):
        # Stock +2%, SPY +10% → alpha -8, even though the raw return is positive.
        rows = [_row("buy", 100.0, 102.0, 100.0, 110.0)]
        scored = _score_rows(rows, "congress")
        alphas = scored[0][-len(WINDOWS):]
        assert all(a == pytest.approx(-8.0) for a in alphas)

    def test_sell_before_a_drop_scores_positive(self):
        # The core inversion: stock -20% while SPY +5%. Selling was a good call,
        # so alpha must be +25, not -25.
        rows = [_row("sell", 100.0, 80.0, 100.0, 105.0)]
        scored = _score_rows(rows, "congress")
        alphas = scored[0][-len(WINDOWS):]
        assert all(a == pytest.approx(25.0) for a in alphas)

    def test_sell_before_a_rally_scores_negative(self):
        rows = [_row("sell", 100.0, 130.0, 100.0, 110.0)]
        scored = _score_rows(rows, "congress")
        alphas = scored[0][-len(WINDOWS):]
        assert all(a == pytest.approx(-20.0) for a in alphas)

    def test_missing_benchmark_yields_null_alpha_not_raw_return(self):
        # If SPY is unavailable we know the return but NOT the excess. Emitting
        # the raw return as alpha would silently inflate every score.
        rows = [_row("buy", 100.0, 120.0, None, None)]
        scored = _score_rows(rows, "congress")
        n = len(WINDOWS)
        rets = scored[0][-2 * n: -n]
        alphas = scored[0][-n:]
        assert all(r == pytest.approx(20.0) for r in rets)
        assert all(a is None for a in alphas)

    def test_row_without_entry_price_is_dropped(self):
        rows = [_row("buy", None, 120.0, 100.0, 105.0)]
        assert _score_rows(rows, "congress") == []

    def test_hold_and_initial_are_not_scored(self):
        # 'hold' exists only to complete the 13F diff; 'initial' is a first
        # sighting of a filer's inventory. Neither is a decision.
        rows = [
            _row("hold", 100.0, 120.0, 100.0, 105.0),
            _row("initial", 100.0, 120.0, 100.0, 105.0),
        ]
        assert _score_rows(rows, "fund") == []

    def test_congress_size_parsed_from_bracket_string(self):
        rows = [_row("buy", 100.0, 120.0, 100.0, 105.0, size_raw="$1,001 - $15,000")]
        scored = _score_rows(rows, "congress")
        assert scored[0][7] == pytest.approx(8000.5)   # size_est_usd
        assert scored[0][8] == CONFIDENCE_RANGE        # size_confidence

    def test_fund_size_passes_through_unchanged(self):
        # 13F reports an exact dollar value; there is no bracket to parse.
        rows = [_row("buy", 100.0, 120.0, 100.0, 105.0, size_est=4_200_000.0)]
        scored = _score_rows(rows, "fund")
        assert scored[0][7] == pytest.approx(4_200_000.0)
