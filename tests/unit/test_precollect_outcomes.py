"""A collector that returns no data must be recorded as an error, not `ok`.

2026-07-26 (cycle-v3-1785107795): every price provider failed for all 12
tickers — yfinance rejected each frame, FMP fell over behind it — and the
cycle summary still read collector_ok=49, collector_error=0,
collector_failures=[]. The reason is that `run_with_telemetry` treated "did
not raise" as success, while the whole price path signals failure by RETURN
VALUE (collect_price_history -> 0, collect_fundamentals -> False,
data_rotator catches provider exceptions and returns 0). A total data outage
was therefore indistinguishable from a clean run in the one table an operator
reads to decide whether a cycle can be trusted.
"""

import pytest

from app.v3 import data_report


def _outcomes_for(results: dict[str, object], deadline_passed: bool = False) -> dict[str, str]:
    """Classify canned collector results through the PRODUCTION rule.

    This used to re-implement `run_with_telemetry`'s decision logic inline,
    which meant the test asserted against a copy and could not observe a change
    to the real wrapper — it kept passing while the wrapper emitted `_ok` for
    collectors that had blown the deadline. The rule now lives in one
    module-level function and the test drives that.
    """
    return {
        name: data_report.classify_collector_outcome(name, value, deadline_passed)[0]
        for name, value in results.items()
    }


def test_zero_price_rows_is_an_error_not_ok():
    outcomes = _outcomes_for({"yfinance_price": 0})
    assert outcomes["yfinance_price"] == "error"


def test_false_fundamentals_is_an_error_not_ok():
    outcomes = _outcomes_for({"yfinance_fund": False})
    assert outcomes["yfinance_fund"] == "error"


def test_real_rows_still_report_ok():
    outcomes = _outcomes_for({"yfinance_price": 124, "yfinance_fund": True})
    assert outcomes == {"yfinance_price": "ok", "yfinance_fund": "ok"}


def test_zero_articles_is_not_an_error():
    """A quiet news day is not a collector failure — only the price path is
    gated on truthiness. Widening _EXPECT_TRUTHY to the news/social collectors
    would recreate the false-alarm problem that made the counters permissive
    in the first place."""
    outcomes = _outcomes_for({"finnhub_news": 0, "reddit": 0, "youtube": 0})
    assert set(outcomes.values()) == {"ok"}


def test_expect_truthy_covers_exactly_the_return_value_collectors():
    assert data_report._EXPECT_TRUTHY == {"yfinance_price", "yfinance_fund"}


# ── Post-deadline stragglers ────────────────────────────────────────────────
# cycle-v3-1785137616: multi_api_news timed out on 6 of 7 tickers and youtube
# on 5 of 7, yet the event stream carried `_ok` for all 7 of each. SBUX's
# `multi_api_news_ok` landed at 07:39:30 — 117s past the 90s deadline, long
# after the report had gone to the agents.

def test_collector_finishing_after_the_deadline_is_late_not_ok():
    outcomes = _outcomes_for({"multi_api_news": 12}, deadline_passed=True)
    assert outcomes["multi_api_news"] == "late"


def test_late_completion_emits_a_warning_not_an_ok_event():
    """The event a dashboard reads must not say success for unused data."""
    _, step, status, detail = data_report.classify_collector_outcome(
        "multi_api_news", 12, deadline_passed=True
    )
    assert step == "late"
    assert status == "warning"
    assert "AFTER the deadline" in detail


def test_on_time_completion_still_emits_ok():
    _, step, status, _ = data_report.classify_collector_outcome(
        "multi_api_news", 12, deadline_passed=False
    )
    assert (step, status) == ("ok", "ok")


def test_no_data_outranks_late():
    """A collector that returned nothing is an error whenever it finished —
    'late' must not launder an empty price frame into a softer status."""
    outcome, step, status, _ = data_report.classify_collector_outcome(
        "yfinance_price", 0, deadline_passed=True
    )
    assert (outcome, step, status) == ("error", "err", "error")
