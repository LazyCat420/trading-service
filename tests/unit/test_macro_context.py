"""Macro backdrop block in agent context — 2026-07-18 macro wave.

The desk was macro-blind: macro_indicators fed only the dashboard. These
tests pin the prompt block's shape and its inclusion in build_memory_addenda.
"""
import datetime as dt
import contextlib
from unittest.mock import patch

from app.services.retrieval_context import build_macro_block, build_memory_addenda


_TODAY = dt.date(2026, 7, 16)


def _rows(**vals):
    return [(k, _TODAY, v) for k, v in vals.items()]


@contextlib.contextmanager
def _mock_mongo(latest_rows, yoy_rows, fail=False):
    """Patch the Mongo layer build_macro_block reads through.

    The latest-per-indicator DISTINCT ON is now a mongo_store.aggregate over
    `macro_indicators`; the YoY self-join became one mongo_query.scalar lookup
    per CPI/PCE_CORE series at the exact date minus one year.
    """
    def _aggregate(collection, pipeline, *a, **k):
        if fail:
            raise RuntimeError("down")
        assert collection == "macro_indicators"
        return [
            {"indicator": ind, "date": date, "value": val}
            for ind, date, val in latest_rows
        ]

    yoy = dict(yoy_rows)

    def _scalar(collection, query, column, *a, **k):
        assert collection == "macro_indicators"
        return yoy.get(query.get("indicator"))

    with patch("app.db.mongo_store.aggregate", side_effect=_aggregate), \
         patch("app.db.mongo_query.scalar", side_effect=_scalar):
        yield


class TestMacroBlock:
    def test_block_renders_rates_inflation_labor(self):
        with _mock_mongo(
            _rows(TREASURY_10Y=4.57, TREASURY_2Y=4.16, FED_FUNDS=3.63,
                  UNEMPLOYMENT=4.2, INITIAL_CLAIMS=208000.0, CPI=332.5,
                  INFLATION_EXPECT=2.27),
            [("CPI", 323.0)],
        ):
            block = build_macro_block("NVDA")
        assert "### Macro Backdrop" in block
        assert "Fed funds 3.63%" in block
        assert "curve 10Y-2Y +0.41pp" in block
        assert "CPI YoY 2.9%" in block
        assert "initial claims 208k" in block
        assert "INVERTED" not in block

    def test_inverted_curve_flagged(self):
        with _mock_mongo(_rows(TREASURY_10Y=4.0, TREASURY_2Y=4.5), []):
            block = build_macro_block("NVDA")
        assert "INVERTED" in block

    def test_empty_table_returns_empty(self):
        with _mock_mongo([], []):
            assert build_macro_block("NVDA") == ""

    def test_db_failure_is_nonfatal(self):
        with _mock_mongo([], [], fail=True):
            assert build_macro_block("NVDA") == ""

    def test_addenda_includes_macro_block(self):
        from app.services import retrieval_context as rc
        with patch.object(rc, "build_working_memory_block", return_value=""), \
             patch.object(rc, "build_retrieved_context", return_value=""), \
             patch.object(rc, "build_brain_graph_block", return_value=""), \
             patch.object(rc, "build_macro_block", return_value="### Macro Backdrop (FRED, latest)"):
            assert "Macro Backdrop" in build_memory_addenda("NVDA")
