"""`technicals` is recomputed, not migrated — and the coverage check is the gate.

`technicals` is 1.37M rows of DERIVED data. Copying it from Postgres would move
the OUTPUT of a computation whose INPUT is already being migrated, carrying
across whatever those rows happen to hold: indicators computed from a vendor
mix, from a window that has since changed, by a `ta` version nobody recorded.

So the pass criterion cannot be "the row counts match". It is per-ticker
COVERAGE against `price_history`, because the row count is a function of the
window — `compute_technicals(period=N)` reads N sessions and drops the first 13
for RSI's warm-up — and the window is a choice.

Two ways a coverage check like this reports success without earning it, both
pinned below: counting a two-vendor ticker's rows twice (which halves its
apparent coverage, or hides a real shortfall behind a doubled denominator), and
treating a ticker with too little history as covered because it has no
technicals to be missing.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

spec = importlib.util.spec_from_file_location(
    "recompute_technicals", _ROOT / "scripts" / "recompute_technicals.py")
rt = importlib.util.module_from_spec(spec)
sys.modules["recompute_technicals"] = rt
spec.loader.exec_module(rt)


@pytest.fixture
def store(monkeypatch):
    """`mongo_store.aggregate` answering from a canned $group result."""
    canned: dict[str, list] = {}

    def aggregate(collection, pipeline, session=None):
        return canned.get(collection, [])

    monkeypatch.setattr(rt.mongo_store, "aggregate", aggregate)
    return canned


def _price(pairs):
    return [{"_id": {"ticker": t, "source": s}, "n": n} for t, s, n in pairs]


def test_a_two_vendor_ticker_counts_its_dominant_vendor_once(store):
    """price_history is keyed (ticker, date, source). Summing rows would call
    AAPL 800-sessions-deep when both vendors carry the same 400 days — and
    `compute_technicals` only ever reads one vendor, so the denominator has to
    be measured the way the numerator is produced."""
    store["price_history"] = _price([
        ("AAPL", "yfinance", 400), ("AAPL", "polygon", 380),
        ("MSFT", "yfinance", 300),
    ])
    assert rt._price_coverage() == {"AAPL": 400, "MSFT": 300}


def test_a_ticker_below_the_minimum_is_not_counted_as_covered(store):
    """`compute_technicals` refuses under 28 sessions, so such a ticker will
    never have technicals. Counting it as MISSING would make the gate
    permanently red; counting it as covered would hide real gaps behind it."""
    store["price_history"] = _price([("TINY", "yfinance", 10),
                                     ("BIG", "yfinance", 600)])
    store["technicals"] = [{"_id": "BIG", "n": 488}]
    assert rt.verify(period=500) == 0


def test_a_ticker_with_no_technicals_fails_the_gate(store):
    store["price_history"] = _price([("BIG", "yfinance", 600)])
    store["technicals"] = []
    assert rt.verify(period=500) == 1


def test_a_short_ticker_fails_the_gate(store):
    """Half a window is not coverage. The 5% tolerance is for warm-up and
    holiday edges, not for a ticker that stopped halfway."""
    store["price_history"] = _price([("BIG", "yfinance", 600)])
    store["technicals"] = [{"_id": "BIG", "n": 200}]
    assert rt.verify(period=500) == 1


def test_the_expected_count_follows_the_window_not_the_history(store):
    """600 sessions with period=500 yields 500 - 27 rows, not 600 - 27. A gate
    that expected the full history would fail every long-lived ticker."""
    store["price_history"] = _price([("BIG", "yfinance", 600)])
    store["technicals"] = [{"_id": "BIG", "n": 500 - (rt._MIN_SESSIONS - 1)}]
    assert rt.verify(period=500) == 0


def test_a_short_history_is_capped_by_what_exists(store):
    """A ticker with 100 sessions cannot produce 473 rows at period=500. The
    expectation is min(window, history) — otherwise every young ticker reads as
    permanently short."""
    store["price_history"] = _price([("YOUNG", "yfinance", 100)])
    store["technicals"] = [{"_id": "YOUNG", "n": 100 - (rt._MIN_SESSIONS - 1)}]
    assert rt.verify(period=500) == 0


def test_technicals_for_a_ticker_with_no_price_history_is_reported(store, capsys):
    """An orphan is not a coverage failure — it is a stale row that survived a
    ticker leaving the universe. Reported, not counted against the gate, so it
    gets attributed rather than silently cleaned up."""
    store["price_history"] = _price([("BIG", "yfinance", 600)])
    store["technicals"] = [{"_id": "BIG", "n": 473}, {"_id": "GONE", "n": 12}]
    rc = rt.verify(period=500)
    out = capsys.readouterr().out
    assert "ORPHAN  GONE" in out
    assert rc == 0
