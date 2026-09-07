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
    # These tests isolate spend/enqueue accounting. Enforced scoring is
    # covered by test_watch_allocator_replay and the real Mongo sweep test.
    monkeypatch.setattr("app.services.watch_allocator.get_mode", lambda: 0)
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
    # The deferral now carries WHY. It used to read "LLY(news)", which could not
    # distinguish an exhausted budget from an allocator refusal or a lost
    # enqueue race — three states needing three different responses. Assert the
    # parts, plus the specific reason, rather than pinning the whole string:
    # a test that pins a formatted constant goes red for having been improved.
    assert len(deferred) == 1
    entry = deferred[0]
    assert entry.startswith("LLY(news:")
    assert "global_daily_budget_exhausted" in entry


def test_a_sweep_loser_is_not_reported_as_a_budget_deferral(spend_env):
    """Losing the sweep is not being deferred.

    `deferred`'s only consumer logs "daily wake budget (N) spent — deferred M
    trip(s)". A candidate that merely ranked below this sweep's winner stays
    armed and re-competes in 15 minutes; filing it as a deferral would fire the
    desk's one genuine saturation warning any time two watches trip at once,
    and a warning that fires constantly is a warning nobody reads.
    """
    enqueued, marked = spend_env(accept_first_n=6)
    deferred = []
    fired, budget_left = asyncio.run(
        watch_desk._spend_wake_budget([_cand("LLY"), _cand("JPM"), _cand("PFE")],
                                      budget_left=6, deferred=deferred)
    )
    assert fired == 1
    assert len(marked) == 1
    assert deferred == []          # two losers, zero deferrals reported
