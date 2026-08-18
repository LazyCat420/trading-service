"""Macro backdrop block in agent context — 2026-07-18 macro wave.

The desk was macro-blind: macro_indicators fed only the dashboard. These
tests pin the prompt block's shape and its inclusion in build_memory_addenda.
"""
import datetime as dt
from unittest.mock import MagicMock, patch

from app.services.retrieval_context import build_macro_block, build_memory_addenda


def _mock_db(latest_rows, yoy_rows):
    db = MagicMock()
    db.execute.side_effect = [
        MagicMock(fetchall=MagicMock(return_value=latest_rows)),
        MagicMock(fetchall=MagicMock(return_value=yoy_rows)),
    ]
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=ctx)


_TODAY = dt.date(2026, 7, 16)


def _rows(**vals):
    return [(k, _TODAY, v) for k, v in vals.items()]


class TestMacroBlock:
    def test_block_renders_rates_inflation_labor(self):
        get_db = _mock_db(
            _rows(TREASURY_10Y=4.57, TREASURY_2Y=4.16, FED_FUNDS=3.63,
                  UNEMPLOYMENT=4.2, INITIAL_CLAIMS=208000.0, CPI=332.5,
                  INFLATION_EXPECT=2.27),
            [("CPI", 323.0)],
        )
        with patch("app.db.connection.get_db", get_db):
            block = build_macro_block("NVDA")
        assert "### Macro Backdrop" in block
        assert "Fed funds 3.63%" in block
        assert "curve 10Y-2Y +0.41pp" in block
        assert "CPI YoY 2.9%" in block
        assert "initial claims 208k" in block
        assert "INVERTED" not in block

    def test_inverted_curve_flagged(self):
        get_db = _mock_db(_rows(TREASURY_10Y=4.0, TREASURY_2Y=4.5), [])
        with patch("app.db.connection.get_db", get_db):
            block = build_macro_block("NVDA")
        assert "INVERTED" in block

    def test_empty_table_returns_empty(self):
        get_db = _mock_db([], [])
        with patch("app.db.connection.get_db", get_db):
            assert build_macro_block("NVDA") == ""

    def test_db_failure_is_nonfatal(self):
        with patch("app.db.connection.get_db", side_effect=RuntimeError("down")):
            assert build_macro_block("NVDA") == ""

    def test_addenda_includes_macro_block(self):
        from app.services import retrieval_context as rc
        with patch.object(rc, "build_working_memory_block", return_value=""), \
             patch.object(rc, "build_retrieved_context", return_value=""), \
             patch.object(rc, "build_brain_graph_block", return_value=""), \
             patch.object(rc, "build_macro_block", return_value="### Macro Backdrop (FRED, latest)"):
            assert "Macro Backdrop" in build_memory_addenda("NVDA")
