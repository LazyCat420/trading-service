"""A NaN-Volume forming bar must not discard the whole frame.

yfinance's mid-session bar (the market is open, today's session hasn't
settled) carries valid OHLC but a NaN Volume — there's no final volume until
the session closes. That row used to survive the OHLC-only dropna in both
fetch_ohlcv_dataframe and collect_price_history, then blow up
PriceHistorySchema's `Volume: Series[int]` coercion — rejecting all 125 rows
over one NaN cell.

Reproduced against a live yfinance frame on 2026-08-01 (see the handoff/audit
report). This is the most likely cause of the opening-bell failures measured
07-27..07-31 (15 of 18 `yfinance_price returned no data` events inside the
09:xx ET hour) — the market-open cycle runs while today's bar is still
forming, and Volume is exactly the field that hasn't settled yet.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pandera.errors
import pytest

from app.collectors import yfinance_collector as yc
from app.validation.schema import PriceHistorySchema


def _run(coro):
    return asyncio.run(coro)


def _six_month_frame(n: int = 125) -> pd.DataFrame:
    idx = pd.date_range("2026-02-01", periods=n, freq="B")
    base = np.arange(n, dtype=float) + 100.0
    return pd.DataFrame(
        {
            "Open": base,
            "High": base + 1.0,
            "Low": base - 1.0,
            "Close": base + 0.5,
            "Volume": np.full(n, 1_000_000, dtype=float),
        },
        index=idx,
    )


class _FakeTicker:
    def __init__(self, result):
        self._result = result

    def history(self, *_a, **_kw):
        return self._result


@pytest.fixture
def frame_with_nan_volume_today():
    """A clean 125-row frame where only the NEWEST bar has NaN Volume,
    valid OHLC otherwise — exactly yfinance's mid-session shape."""
    df = _six_month_frame()
    df.loc[df.index[-1], "Volume"] = np.nan
    return df


def test_a_forming_bar_with_nan_volume_does_not_reach_the_schema(
    monkeypatch, frame_with_nan_volume_today
):
    """fetch_ohlcv_dataframe must drop the bad row, not keep it.

    Before the fix this row survived the dropna (Volume wasn't in the
    subset) and was returned to the caller, where it broke schema
    validation for the whole frame.
    """
    monkeypatch.setattr(
        yc.yf, "Ticker", lambda _t: _FakeTicker(frame_with_nan_volume_today)
    )
    out = _run(yc.fetch_ohlcv_dataframe("AAPL"))
    assert out is not None
    assert len(out) == 124, "the NaN-volume bar must be dropped, not kept"
    assert not out["Volume"].isna().any()


def test_the_salvaged_frame_passes_schema_validation(frame_with_nan_volume_today):
    """The regression this bug caused: schema validation must not reject
    the whole frame over one bad Volume cell, once that cell is dropped
    the way fetch_ohlcv_dataframe now drops it."""
    clean = frame_with_nan_volume_today.dropna(
        subset=["Open", "High", "Low", "Close", "Volume"]
    )
    assert len(clean) == 124
    validated = PriceHistorySchema.validate(clean)
    assert len(validated) == 124


def test_the_bug_as_it_shipped_rejects_the_entire_frame(frame_with_nan_volume_today):
    """Documents the exact failure this fix closes: the OLD dropna subset
    (OHLC only, no Volume) leaves the NaN-volume row in, and schema
    validation then rejects every row — not just the bad one."""
    old_subset_result = frame_with_nan_volume_today.dropna(
        subset=["Open", "High", "Low", "Close"]
    )
    assert len(old_subset_result) == 125, "the NaN-volume row survives the old filter"
    with pytest.raises((pandera.errors.SchemaError, pandera.errors.SchemaErrors)):
        PriceHistorySchema.validate(old_subset_result)


def test_collect_price_history_salvages_rather_than_discards(monkeypatch, frame_with_nan_volume_today):
    """End-to-end: collect_price_history must return 124, not 0."""
    monkeypatch.setattr(
        yc.yf, "Ticker", lambda _t: _FakeTicker(frame_with_nan_volume_today)
    )
    monkeypatch.setattr(yc, "_is_blocked_ticker", lambda _t: False)

    inserted_rows = []

    # Writes go through mongo_store.upsert_doc("price_history", key, doc,
    # insert_only=True) — one call per bar. Capturing the docs keeps the
    # original check ("how many rows actually got written") intact.
    store = MagicMock()

    def _upsert(collection, key, doc, **_kw):
        assert collection == "price_history"
        inserted_rows.append(doc)

    store.upsert_doc.side_effect = _upsert
    monkeypatch.setattr(yc, "mongo_store", store)

    async def _noop_refresh(*_a, **_kw):
        return None

    monkeypatch.setattr(yc, "_refresh_technicals", _noop_refresh)

    count = _run(yc.collect_price_history("AAPL"))
    assert count == 124, "one bad Volume cell must not zero out the whole collection"
    # The returned count must reflect rows that were actually written, not a
    # tally computed beside an empty write path.
    assert len(inserted_rows) == 124
    assert all(isinstance(d["volume"], int) for d in inserted_rows)
