"""One sweep spends at most one wake, and only on an ACCEPTED enqueue.

Regression for the burned-budget defect measured 2026-07-28 → 08-03: six
same-second trips each enqueued a START_CYCLE, all six were marked fired
(budget spent, cooldown started, last_fired_at advanced past the headline),
but cycle_main's serial poller could only ever run one — exactly 1 completed
/ 5 skipped per day for seven straight days, 83% of the desk's autonomy.
"""

import asyncio

import pytest

from app.services import watch_desk


def _cand(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "trig": {"type": "news"},
        "watch": {"id": 1, "ticker": ticker, "fire_count": 0},
        "detail": f"{ticker} material news",
        "value": None,
    }


@pytest.fixture
def spend_env(monkeypatch):
    enqueued, marked = [], []
    monkeypatch.setattr(watch_desk, "_held_tickers", lambda: set())
    monkeypatch.setattr(
        watch_desk, "_mark_fired",
        lambda watch, trig, detail, value, cycle_id: marked.append(watch["ticker"]),
    )

    def arm(accept_first_n: int):
        async def fake_enqueue(watch, trig, detail):
            enqueued.append(watch["ticker"])
            return f"wd-{watch['ticker']}" if len(enqueued) <= accept_first_n else None
        monkeypatch.setattr(watch_desk, "_enqueue_wake", fake_enqueue)
        return enqueued, marked

    return arm


def test_a_burst_of_six_spends_exactly_one_wake(spend_env):
    enqueued, marked = spend_env(accept_first_n=6)
    deferred = []
    fired, budget_left = asyncio.run(
        watch_desk._spend_wake_budget([_cand(t) for t in
                                       ("LLY", "HOOD", "JPM", "BTC", "PFE", "MCD")],
                                      budget_left=6, deferred=deferred)
    )
    assert fired == 1
    assert budget_left == 5
    assert len(marked) == 1
    # The five losers are neither marked fired nor logged as budget-deferred —
    # they stay armed and re-trip on the next sweep.
    assert deferred == []


def test_a_rejected_enqueue_burns_nothing_and_tries_the_next_candidate(spend_env):
    enqueued, marked = spend_env(accept_first_n=0)
    deferred = []
    fired, budget_left = asyncio.run(
        watch_desk._spend_wake_budget([_cand("LLY"), _cand("JPM")],
                                      budget_left=6, deferred=deferred)
    )
    assert fired == 0
    assert budget_left == 6
    assert marked == []
    assert enqueued == ["LLY", "JPM"]  # tried both, burned neither


def test_exhausted_budget_defers_instead_of_enqueueing(spend_env):
    enqueued, marked = spend_env(accept_first_n=6)
    deferred = []
    fired, budget_left = asyncio.run(
        watch_desk._spend_wake_budget([_cand("LLY")], budget_left=0,
                                      deferred=deferred)
    )
    assert fired == 0
    assert enqueued == []
    assert deferred == ["LLY(news)"]
