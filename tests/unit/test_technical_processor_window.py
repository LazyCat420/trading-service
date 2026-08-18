"""`technicals` must be computed from the NEWEST prices, not the oldest.

The bug this pins (found 2026-07-25): `compute_technicals` selected
`ORDER BY date ASC LIMIT 500` — the OLDEST 500 sessions. For MSFT (10,169 rows
back to 1986) every run recomputed 1986-03-13 .. 1988-03-03 and never touched a
recent date. CVX's newest technical row was **1963-12-26** against a 2026-07-24
price: a 22,856-day lag, served to the quant analyst as its "verified
technical baseline".

Compounding it, `ON CONFLICT (ticker, date) DO NOTHING` meant a re-run could
never correct an existing row, so the damage could only accumulate.

WHAT CHANGED WITH MONGO
-----------------------
The processor reads `mongo_store.find_docs("price_history", ...)` and writes
through `mongo_store.upsert_doc("technicals", key, doc)`. There is no SQL text
left to grep, so each assertion below now names the STRUCTURE that carries the
same guarantee: the `sort` direction, the `limit` value, the query filter, the
upsert key, and how many write calls the run makes. Patching
`technical_processor.get_db` intercepted nothing after the conversion — the
module has no `get_db` — so every one of these tests was failing outright.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest


def _prices(start: dt.date, n: int) -> list[dict]:
    """n consecutive daily bars with a gently rising close, NEWEST FIRST.

    `find_docs` is called with `sort=[("date", -1)]`, so the fake returns them
    in the order that sort implies — the processor's own `.reverse()` is then
    doing real work, and a test that handed back ascending rows would score a
    silently reversed frame as correct.
    """
    out = []
    for i in range(n):
        d = start + dt.timedelta(days=i)
        base = 100.0 + i * 0.1
        out.append({
            "date": d, "open": base, "high": base + 1.0, "low": base - 1.0,
            "close": base + 0.5, "volume": 1_000_000, "source": "yfinance",
        })
    return list(reversed(out))


class _FakeStore:
    """Stands in for `app.db.mongo_store`, recording the calls it receives."""

    def __init__(self, docs):
        self._docs = docs
        self.find_calls: list[dict] = []
        self.aggregates: list[dict] = []
        self.upserts: list[tuple[str, dict, dict]] = []
        self.bulk_upserts: list[tuple[str, list, str]] = []

    #: Vendors the fake reports for the ticker. Two by default, because the
    #: bug this file pins only exists on a DUAL-source ticker: with one vendor
    #: the pin is a no-op and every assertion about it passes vacuously.
    #: "yfinance" is the dominant one — more rows, same freshness.
    sources = [
        {"_id": "yfinance", "n": 300, "mx": dt.date(2024, 10, 26)},
        {"_id": "polygon", "n": 120, "mx": dt.date(2024, 10, 26)},
    ]

    def aggregate(self, collection, pipeline, session=None):
        """Only the vendor-census shape the processor issues."""
        self.aggregates.append({"collection": collection, "pipeline": pipeline})
        return list(self.sources)

    def find_docs(self, collection, query, sort=None, projection=None,
                  limit=0, session=None):
        self.find_calls.append({
            "collection": collection, "query": query, "sort": sort,
            "limit": limit,
        })
        docs = self._docs
        if limit:
            docs = docs[:limit]
        # Copies: the processor mutates the list it gets back (`.reverse()`).
        return [dict(d) for d in docs]

    def upsert_doc(self, collection, key, doc, insert_only=False, session=None):
        self.upserts.append((collection, key, doc))
        assert not insert_only, (
            "insert_only mirrors ON CONFLICT DO NOTHING — a re-run could then "
            "never correct a wrong row"
        )

    def bulk_upsert(self, collection, docs, key_field="id"):
        self.bulk_upserts.append((collection, list(docs), key_field))
        return len(docs)

    # ── the write the technicals path must never make ────────────────
    def insert_docs(self, collection, docs, **kwargs):  # pragma: no cover
        raise AssertionError(f"plain insert into {collection} cannot correct a row")

    @property
    def write_calls(self) -> int:
        """Round-trips to the store: per-row upserts plus batched writes."""
        return len(self.upserts) + len(self.bulk_upserts)

    @property
    def rows_written(self) -> int:
        return len(self.upserts) + sum(len(d) for _c, d, _k in self.bulk_upserts)


def _run(docs, period=500):
    import app.processors.technical_processor as tp

    store = _FakeStore(docs)
    with patch.object(tp, "mongo_store", store):
        written = tp.compute_technicals("TEST", period=period)
    return store, written


class TestWindowIsTheRecentEnd:
    def test_query_orders_descending_before_limiting(self):
        """The LIMIT must apply to the NEWEST rows. Ordering ascending first
        is what selected 1986 data for a 2026 cycle."""
        store, _ = _run(_prices(dt.date(2024, 1, 1), 300))
        call = store.find_calls[0]
        assert call["collection"] == "price_history"
        assert call["sort"] == [("date", -1)], (
            "the limit must be applied to the most recent sessions"
        )

    def test_rows_are_resorted_ascending_for_indicator_math(self):
        """Every `ta` indicator is order-dependent, so the window has to be
        handed to pandas chronologically.

        The fake returns rows newest-first, matching the descending sort the
        processor asks for; the frame it computes on must come out ascending.
        A reversed frame is not a crash — it is quietly wrong indicators, so
        this reads the DATES the writer actually emitted rather than any query
        text."""
        store, written = _run(_prices(dt.date(2024, 1, 1), 300))
        assert written > 0
        # Read the documents from WHICHEVER write path ran. The processor
        # batches now (one bulk_upsert instead of ~287 per-row upserts), and a
        # test that only inspected `upserts` would silently see an empty list
        # and fail on max() rather than on the ordering it is checking.
        dates = [doc["date"] for _c, _k, doc in store.upserts]
        for _c, docs, _k in store.bulk_upserts:
            dates.extend(d["date"] for d in docs)
        assert dates == sorted(dates), "indicator rows came out non-chronological"
        # And the window is the RECENT end: the last bar generated is present.
        assert max(dates) == dt.date(2024, 1, 1) + dt.timedelta(days=299)

    def test_period_is_passed_as_the_limit(self):
        """`period` must reach the query as the row cap, or the window is
        whatever the collection happens to hold."""
        store, _ = _run(_prices(dt.date(2024, 1, 1), 300))
        assert store.find_calls[0]["limit"] == 500, (
            f"period must be bound as the limit, got {store.find_calls[0]!r}"
        )

    def test_the_window_pins_one_vendor(self):
        """`source` is part of the price_history key, so an unfiltered window
        returns `period` ROWS over ~period/2 DATES on a dual-source ticker, and
        mixes adjusted with raw closes. Both corrupt every indicator written
        here."""
        store, _ = _run(_prices(dt.date(2024, 1, 1), 300))
        query = store.find_calls[0]["query"]
        assert "source" in query, (
            "the indicator window must pin one vendor — price_history is keyed "
            f"(ticker, date, source), got {query!r}"
        )
        # And it must pin the DOMINANT one, not merely any one: the fake
        # reports yfinance with 300 rows against polygon's 120 at equal
        # freshness, so picking polygon would satisfy "source is present"
        # while still reading the thinner series.
        assert query["source"] == "yfinance", (
            f"pinned the wrong vendor: {query['source']!r}"
        )


class TestUpsertCanCorrectExistingRows:
    def test_conflict_updates_rather_than_skipping(self):
        """DO NOTHING made the table append-only: a re-run could never repair
        a wrong row, which is why 62-year-old values survived every refresh.

        The Mongo equivalent is `$set` on the natural key, i.e. `upsert_doc`
        WITHOUT `insert_only` (which is the `$setOnInsert` / DO NOTHING
        semantics) — the fake asserts that flag stays off. Here: the key is the
        real natural key, and the payload really does overwrite the indicator
        columns."""
        store, _ = _run(_prices(dt.date(2024, 1, 1), 300))
        assert store.rows_written, "expected indicator rows to be written"

        if store.upserts:
            collection, key, doc = store.upserts[0]
        else:  # batched path
            collection, docs, _key_field = store.bulk_upserts[0]
            doc = docs[0]
            key = {"ticker": doc["ticker"], "date": doc["date"]}

        assert collection == "technicals"
        assert set(key) == {"ticker", "date"}, (
            f"technicals is keyed (ticker, date), upserted on {key!r}"
        )
        assert key["ticker"] == "TEST"
        assert "rsi_14" in doc and doc["rsi_14"] is not None


class TestWritesAreBatched:
    def test_all_rows_go_in_one_executemany(self):
        """One statement per row cost 22.6s/ticker, which turned repairing the
        universe into a ~16h job. Keep the write batched."""
        store, written = _run(_prices(dt.date(2024, 1, 1), 300))
        assert store.rows_written == written
        assert store.write_calls == 1, (
            f"expected a single batched write, made {store.write_calls} "
            f"round-trips for {written} rows"
        )


class TestGuards:
    @pytest.mark.parametrize("n", [0, 1, 4, 9, 15, 24, 27])
    def test_too_little_history_skips_cleanly(self, n):
        """`ta` RAISES on a short frame rather than returning NaN, so a thin
        ticker must be skipped, not attempted. The old >=5 floor let 12
        tickers (9-24 rows) crash the writer mid-backfill."""
        store, written = _run(_prices(dt.date(2024, 1, 1), n))
        assert written == 0
        assert store.write_calls == 0

    def test_28_sessions_is_enough(self):
        """ADX smooths an already-smoothed series, so at window=14 it needs
        ~2x the window: measured, it raises at 25 rows and succeeds at 28."""
        store, written = _run(_prices(dt.date(2024, 1, 1), 28))
        assert written > 0
        assert store.rows_written == written
