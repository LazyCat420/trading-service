"""BUY sizing semantics — regression guard for the cash-vs-equity base.

The agents decide position_size_pct in PORTFOLIO terms; sizing used to be
computed as `cash * size_pct`, so with cash at ~8% of the book an intended
2.5% position executed at ~0.2% of equity (observed live: a $224 DIS fill on
a $100k book). resolve_buy_amount sizes on equity, capped by available cash.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.trading.paper_trader import resolve_buy_amount


def test_sizes_on_equity_not_cash():
    # $100k book, only $8k cash: a 2.5% position must target $2,500 — not $200.
    assert resolve_buy_amount(portfolio_value=100_000, cash=8_000, size_pct=0.025) == 2_500


def test_cash_caps_the_spend():
    # 10% of a $100k book is $10k, but only $8k cash exists → spend the cash.
    assert resolve_buy_amount(portfolio_value=100_000, cash=8_000, size_pct=0.10) == 8_000


def test_size_pct_clamped_to_100_percent():
    assert resolve_buy_amount(portfolio_value=50_000, cash=50_000, size_pct=1.5) == 50_000


def test_all_cash_book_unchanged_semantics():
    # Fresh bot: equity == cash, so behavior matches the old formula exactly.
    assert resolve_buy_amount(portfolio_value=100_000, cash=100_000, size_pct=0.05) == 5_000


def test_zero_cash_yields_zero():
    assert resolve_buy_amount(portfolio_value=100_000, cash=0, size_pct=0.05) == 0
