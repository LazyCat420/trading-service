"""`technicals` must be computed from the NEWEST prices, not the oldest.

The bug this pins (found 2026-07-25): `compute_technicals` selected
`ORDER BY date ASC LIMIT 500` — the OLDEST 500 sessions. For MSFT (10,169 rows
back to 1986) every run recomputed 1986-03-13 .. 1988-03-03 and never touched a
recent date. CVX's newest technical row was **1963-12-26** against a 2026-07-24
price: a 22,856-day lag, served to the quant analyst as its "verified
technical baseline".

Compounding it, `ON CONFLICT (ticker, date) DO NOTHING` meant a re-run could
never correct an existing row, so the damage could only accumulate.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest


def _prices(start: dt.date, n: int) -> list[tuple]:
    """n consecutive daily bars with a gently rising close."""
    out = []
    for i in range(n):
        d = start + dt.timedelta(days=i)
        base = 100.0 + i * 0.1
        out.append((d, base, base + 1.0, base - 1.0, base + 0.5, 1_000_000))
    return out


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.select_sql = ""
        self.select_params = None
        self.inserts: list[str] = []
        self.batch_sizes: list[int] = []

    def execute(self, sql, params=None):
        if "FROM price_history" in sql:
            self.select_sql = sql
            self.select_params = params
            res = MagicMock()
            res.fetchall.return_value = self._rows
            return res
        if "INSERT INTO technicals" in sql:
            self.inserts.append(sql)
        return MagicMock()

    def executemany(self, sql, params_seq):
        # The writer batches its ~490 rows per ticker through executemany;
        # a loop of execute() made a full-universe repair a ~16h job.
        if "INSERT INTO technicals" in sql:
            self.inserts.append(sql)
            self.batch_sizes.append(len(list(params_seq)))
        return MagicMock()


def _run(rows):
    import contextlib

    import app.processors.technical_processor as tp

    db = _FakeDB(rows)

    @contextlib.contextmanager
    def fake_get_db():
        yield db

    with patch.object(tp, "get_db", fake_get_db):
        written = tp.compute_technicals("TEST", period=500)
    return db, written


class TestWindowIsTheRecentEnd:
    def test_query_orders_descending_before_limiting(self):
        """The LIMIT must apply to the NEWEST rows. Ordering ascending first
        is what selected 1986 data for a 2026 cycle."""
        db, _ = _run(_prices(dt.date(2024, 1, 1), 300))
        sql = " ".join(db.select_sql.split())
        assert "ORDER BY date DESC" in sql, (
            "LIMIT must be applied to the most recent sessions"
        )

    def test_rows_are_resorted_ascending_for_indicator_math(self):
        """Every `ta` indicator is order-dependent, so the window has to be
        handed to pandas chronologically."""
        db, _ = _run(_prices(dt.date(2024, 1, 1), 300))
        sql = " ".join(db.select_sql.split())
        # The outer query re-sorts what the inner DESC/LIMIT selected.
        assert sql.rstrip().endswith("ORDER BY date ASC")

    def test_period_is_passed_as_the_limit(self):
        db, _ = _run(_prices(dt.date(2024, 1, 1), 300))
        assert db.select_params[1] == 500


class TestUpsertCanCorrectExistingRows:
    def test_conflict_updates_rather_than_skipping(self):
        """DO NOTHING made the table append-only: a re-run could never repair
        a wrong row, which is why 62-year-old values survived every refresh."""
        db, _ = _run(_prices(dt.date(2024, 1, 1), 300))
        assert db.inserts, "expected indicator rows to be written"
        sql = " ".join(db.inserts[0].split())
        assert "ON CONFLICT (ticker, date) DO UPDATE SET" in sql
        assert "DO NOTHING" not in sql
        assert "rsi_14 = EXCLUDED.rsi_14" in sql


class TestWritesAreBatched:
    def test_all_rows_go_in_one_executemany(self):
        """One statement per row cost 22.6s/ticker, which turned repairing the
        universe into a ~16h job. Keep the write batched."""
        db, written = _run(_prices(dt.date(2024, 1, 1), 300))
        assert len(db.inserts) == 1, "expected a single batched write"
        assert db.batch_sizes == [written]


class TestGuards:
    @pytest.mark.parametrize("n", [0, 1, 4, 9, 15, 24, 27])
    def test_too_little_history_skips_cleanly(self, n):
        """`ta` RAISES on a short frame rather than returning NaN, so a thin
        ticker must be skipped, not attempted. The old >=5 floor let 12
        tickers (9-24 rows) crash the writer mid-backfill."""
        db, written = _run(_prices(dt.date(2024, 1, 1), n))
        assert written == 0
        assert not db.inserts

    def test_28_sessions_is_enough(self):
        """ADX smooths an already-smoothed series, so at window=14 it needs
        ~2x the window: measured, it raises at 25 rows and succeeds at 28."""
        db, written = _run(_prices(dt.date(2024, 1, 1), 28))
        assert written > 0
        assert db.inserts
