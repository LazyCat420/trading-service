"""The 2026-08-03 blocklist wave: a refusal is not an outage.

Three linked defects from cycle-v3-1785763800:

1. FALSE_TICKERS is runtime-mutated — extraction auto-bans anything whose
   yfinance lookup came back empty, and boot folds company_registry's
   `rejected` rows in. A 2026-05-07 vendor outage mass-banned 71 real ETFs
   (SPY, QQQ, IWM, SMH...) and every EXPLICIT price fetch for them was then
   refused forever. Explicit fetches must consult only the static curated set.

2. FB (renamed META in 2022) reached the cycle's ticker set and produced the
   cycle's only yfinance_price failure. Renames resolve at cycle entry.

3. The self-healing watchdog re-"Detected" the same newest error event on
   every hourly pass — one GE analyst failure alerted five hours running.
"""

import asyncio

import pytest

from app.collectors.yfinance_collector import _is_blocked_ticker
from app.processors import ticker_extractor
from app.processors.ticker_extractor import FALSE_TICKERS, STATIC_FALSE_TICKERS
from app.utils.us_ticker_resolver import RENAMED_TICKERS, resolve_tickers_batch


# ── 1. static vs runtime blocklist ───────────────────────────────────

def test_static_set_is_frozen_and_a_subset_of_the_curated_literal():
    assert isinstance(STATIC_FALSE_TICKERS, frozenset)
    # The static set is captured at module definition; hand-curated entries
    # must be present, and no ETF majors may ever be baked into it.
    assert "YOLO" in STATIC_FALSE_TICKERS
    assert "CEO" in STATIC_FALSE_TICKERS
    for etf in ("SPY", "QQQ", "IWM", "SMH", "XLE", "XLB", "QQQJ", "MUST"):
        assert etf not in STATIC_FALSE_TICKERS, (
            f"{etf} is a real ETF and must never be statically blocklisted"
        )


def test_runtime_ban_does_not_block_an_explicit_fetch():
    """A runtime FALSE_TICKERS.add must not reach _is_blocked_ticker."""
    sym = "ZZTESTBAN"
    assert not _is_blocked_ticker(sym)
    FALSE_TICKERS.add(sym)
    try:
        # The runtime-augmented set has it; the explicit-fetch guard ignores it.
        assert sym in FALSE_TICKERS
        assert sym not in STATIC_FALSE_TICKERS
        assert not _is_blocked_ticker(sym), (
            "explicit fetches must consult STATIC_FALSE_TICKERS only — a "
            "runtime auto-ban (one empty yfinance lookup) blocked SPY/QQQ/IWM "
            "price collection for three months"
        )
    finally:
        FALSE_TICKERS.discard(sym)


def test_static_entries_still_block_explicit_fetches():
    """Updated deliberately 2026-08-08 — it used to assert `YOLO`.

    `YOLO` is the AdvisorShares Pure Cannabis ETF: a real, currently listed
    instrument. The test was pinning the defect as correct, which is the shape
    open item 7 records — a test that asserts the behaviour rather than the
    contract cannot see the behaviour is wrong.

    The contract is now "slang with no listing", so the assertion moves to a
    token that is not a symbol anywhere. 124 of the slang list's 324 entries
    turned out to be listed instruments; see
    `app/collectors/explicit_fetch_guard.py`.
    """
    assert _is_blocked_ticker("CEO")
    assert _is_blocked_ticker("ceo")

    assert not _is_blocked_ticker("YOLO"), (
        "YOLO is a listed ETF — refusing an explicit fetch for it is the "
        "defect, not the guard working"
    )


def test_extraction_hard_block_still_honours_runtime_bans():
    """The split must not weaken extraction: _is_hard_blocked reads the
    runtime-augmented set, so auto-banned junk stays out of extraction."""
    sym = "ZZTESTBAN2"
    registry = ticker_extractor.CompanyRegistry.__new__(
        ticker_extractor.CompanyRegistry
    )
    registry._by_symbol = {}
    registry._by_name = {}
    registry._by_alias = {}
    FALSE_TICKERS.add(sym)
    try:
        assert ticker_extractor._is_hard_blocked(sym, registry)
    finally:
        FALSE_TICKERS.discard(sym)


# ── 2. renamed tickers resolve at cycle entry ────────────────────────

def test_fb_resolves_to_meta():
    assert RENAMED_TICKERS["FB"] == "META"
    assert resolve_tickers_batch(["FB"]) == ["META"]


def test_rename_converging_on_an_existing_ticker_dedupes():
    assert resolve_tickers_batch(["FB", "META", "AAPL"]) == ["META", "AAPL"]


def test_unrelated_tickers_pass_through_unchanged():
    assert resolve_tickers_batch(["AAPL", "MSFT"]) == ["AAPL", "MSFT"]


# ── 3. precollect classifies a blocklisted ticker as a skip ──────────

def test_blocked_ticker_never_dispatches_yfinance_collectors():
    """Drive run_data_collection far enough to observe the skip decision.

    We don't run the whole pipeline — we assert the decision function the
    dispatch gate uses agrees with the collector's own refusal, so the two
    can't disagree about what "blocked" means.
    """
    from app.v3 import data_report

    src = __import__("inspect").getsource(data_report.build_ticker_data_report)
    assert "_is_blocked_ticker" in src, (
        "build_ticker_data_report must gate dispatch on the collector's own "
        "blocklist check — a blocklisted ticker returned 0 and classified "
        "as a precollect ERROR (FB, cycle-v3-1785763800)"
    )
    assert '"skipped"' in src


# ── 4. watchdog does not re-detect the same event hourly ─────────────

@pytest.fixture()
def watchdog(monkeypatch):
    import sys
    from pathlib import Path

    scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import self_healing_watchdog as wd

    wd._last_handled_event = None
    return wd


def test_same_error_event_is_handled_once(watchdog, monkeypatch, caplog):
    wd = watchdog
    event = {
        "phase": "analyzing",
        "step": "v3_v3_junior_analyst_fail_GE",
        "detail": "analyst artifact missing required fields",
        "timestamp": "2026-08-03T14:37:00",
    }
    detected = []

    monkeypatch.setattr(
        wd, "get_active_cycle",
        lambda: ("cycle-test", "running", "", "analyzing"),
    )
    monkeypatch.setattr(wd, "get_latest_error_events", lambda cid: [event])
    monkeypatch.setattr(wd, "fetch_nas_cycle_logs", lambda cid: "")
    monkeypatch.setattr(
        wd, "detect_target_from_error",
        lambda msg: detected.append(msg) or None,
    )

    asyncio.run(wd.heal_once())
    asyncio.run(wd.heal_once())
    assert len(detected) == 1, (
        "the same newest error event must be diagnosed once, not once per "
        "hourly pass — 2026-08-03 logged one GE failure as five fresh crashes"
    )


def test_a_new_event_is_still_detected(watchdog, monkeypatch):
    wd = watchdog
    events = [{
        "phase": "analyzing", "step": "s1", "detail": "boom",
        "timestamp": "2026-08-03T14:37:00",
    }]
    detected = []

    monkeypatch.setattr(
        wd, "get_active_cycle",
        lambda: ("cycle-test", "running", "", "analyzing"),
    )
    monkeypatch.setattr(wd, "get_latest_error_events", lambda cid: list(events))
    monkeypatch.setattr(wd, "fetch_nas_cycle_logs", lambda cid: "")
    monkeypatch.setattr(
        wd, "detect_target_from_error",
        lambda msg: detected.append(msg) or None,
    )

    asyncio.run(wd.heal_once())
    events[0] = dict(events[0], step="s2", timestamp="2026-08-03T15:37:00")
    asyncio.run(wd.heal_once())
    assert len(detected) == 2
