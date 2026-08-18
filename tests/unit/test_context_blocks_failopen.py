"""alt_data_block + book_brief must NEVER raise — they degrade to ""/[]."""

from unittest.mock import patch

from app.v3.alt_data_block import build_alt_data_block, alt_macro_lines
from app.v3.book_brief import build_book_brief


def _boom(*a, **k):
    raise RuntimeError("db down")


def test_alt_data_block_failopen():
    with patch("app.v3.alt_data_block.get_db", _boom):
        assert build_alt_data_block("NVDA") == ""


def test_alt_data_block_empty_ticker():
    assert build_alt_data_block("") == ""


def test_alt_macro_lines_failopen():
    with patch("app.v3.alt_data_block.get_db", _boom):
        assert alt_macro_lines() == []


def test_book_brief_failopen():
    with patch("app.trading.paper_trader.get_portfolio", _boom):
        assert build_book_brief("NVDA") == ""


def test_book_brief_all_cash():
    with patch("app.trading.paper_trader.get_portfolio",
               lambda bot_id: {"cash": 10000.0, "positions": []}):
        brief = build_book_brief("NVDA")
        assert "ALL CASH" in brief
