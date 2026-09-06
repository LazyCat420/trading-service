"""Regression tests for cycle_run_summaries counting.

Bug history (2026-07-14, cycle-v3-1784026688): _process_ticker returned None,
so asyncio.gather produced [None, ...] and every count in the run summary
collapsed to 0 even though 3 decisions were persisted and 3 paper orders
executed. summarize_ticker_results now aggregates the returned result dicts.
"""
from app.services.pipeline_service import summarize_ticker_results


def _result(action, attempted=False, executed=False, failed=False):
    return {
        "action": action,
        "confidence": 75,
        "trade_attempted": attempted,
        "trade_executed": executed,
        "trade_failed": failed,
    }


def test_counts_actions_and_trades():
    results = [
        _result("BUY", attempted=True, executed=True),
        _result("SELL", attempted=True, executed=True),
        _result("SELL", attempted=True, failed=True),
        _result("HOLD"),
    ]
    s = summarize_ticker_results(results)
    assert s["analysis_results_count"] == 4
    assert s["buy_count"] == 1
    assert s["sell_count"] == 2
    assert s["hold_count"] == 1
    assert s["trade_attempted"] == 3
    assert s["trade_executed"] == 2
    assert s["trade_failed"] == 1


def test_ignores_none_and_exceptions():
    # gather(return_exceptions=True) mixes Nones (stopped tickers) and
    # Exceptions (crashed tickers) into the result list.
    results = [None, RuntimeError("boom"), _result("BUY")]
    s = summarize_ticker_results(results)
    assert s["analysis_results_count"] == 1
    assert s["buy_count"] == 1


def test_empty_and_none_input():
    assert summarize_ticker_results([])["analysis_results_count"] == 0
    assert summarize_ticker_results(None)["analysis_results_count"] == 0


def test_lowercase_actions_counted():
    s = summarize_ticker_results([_result("buy"), _result("hold")])
    assert s["buy_count"] == 1
    assert s["hold_count"] == 1


def test_missing_action_not_counted_as_hold():
    s = summarize_ticker_results([{"confidence": 10}])
    assert s["analysis_results_count"] == 1
    assert s["hold_count"] == 0


# ── A stopped cycle must still count the trades it made ────────────────────
#
# MEASURED 2026-09-05 (Appendix K.5 of the trading-cycle audit).
# `cycle-v3-1788642086` was stopped at 21:59:22 so a fix could deploy. Before
# the stop, LULU's desk had finished and TRADED:
#
#     21:56:54  v3_done_LULU        BUY @ 71%
#     21:56:54  trade_executed_LULU LULU: BUY 8.4181 @ $100.63739042097185
#
# and the book agrees — `orders`, `trade_fills` (fees 0.23, decision_price
# 100.61, cycle_id set), `position_lots` (open), `positions.LULU` qty 8.418,
# two active stop/take-profit triggers, and a `trade_results` row
# BUY@71/EXECUTE_BUY. The summary for the same cycle says:
#
#     status=stopped  buy_count=0  trade_attempted=0  trade_executed=0
#
# because the per-ticker results exist ONLY as the return value of
# `asyncio.gather(*tasks)`; CancelledError unwinds before the assignment, and
# the handler calls `_persist_summary("stopped", tickers, error=...)` with
# `results=None`. `summarize_ticker_results(None)` then zeroes every count.
# The `error` path at :2838 has the same shape.
#
# Any audit, dashboard or autoresearch report reading `cycle_run_summaries`
# therefore sees a cycle that traded nothing while the portfolio carries its
# position. `scripts/cycle_audit.py --check` printed "0B/0S/0H · 0 executed"
# for a cycle that bought LULU.
#
# The facts are recoverable: `trade_results` and `trade_fills` both carry
# `cycle_id`. These tests pin that recovery.
from unittest.mock import patch

from app.services.pipeline_service import recover_ticker_results_from_store


def _store(trade_results, trade_fills):
    """Stand in for mongo_store.find_docs over the two collections we read."""
    def find_docs(collection, query=None, **kwargs):
        if collection == "trade_results":
            return list(trade_results)
        if collection == "trade_fills":
            return list(trade_fills)
        return []
    return find_docs


def test_a_stopped_cycles_executed_buy_is_recovered_from_the_store():
    """The LULU specimen, reconstructed."""
    trade_results = [{
        "ticker": "LULU", "cycle_id": "cycle-v3-1788642086",
        "action": "BUY", "policy_action": "EXECUTE_BUY", "confidence": 71,
    }]
    trade_fills = [{
        "ticker": "LULU", "cycle_id": "cycle-v3-1788642086",
        "side": "BUY", "fill_qty": 8.418103240335778,
    }]

    with patch("app.services.pipeline_service.mongo_store") as ms:
        ms.find_docs.side_effect = _store(trade_results, trade_fills)
        recovered = recover_ticker_results_from_store("cycle-v3-1788642086")

    s = summarize_ticker_results(recovered)
    assert s["analysis_results_count"] == 1
    assert s["buy_count"] == 1
    assert s["trade_attempted"] == 1
    assert s["trade_executed"] == 1
    assert s["trade_failed"] == 0


def test_a_decision_that_was_blocked_is_recovered_as_blocked_not_executed():
    """A policy-blocked BUY has a trade_results row and NO fill. It must not
    read as executed, and its reason must survive so the skip buckets are
    still meaningful."""
    trade_results = [{
        "ticker": "NVDA", "cycle_id": "c1",
        "action": "BUY", "policy_action": "BLOCKED_CONFIDENCE", "confidence": 55,
    }]

    with patch("app.services.pipeline_service.mongo_store") as ms:
        ms.find_docs.side_effect = _store(trade_results, [])
        recovered = recover_ticker_results_from_store("c1")

    s = summarize_ticker_results(recovered)
    assert s["buy_count"] == 1
    assert s["trade_attempted"] == 0
    assert s["trade_executed"] == 0
    assert recovered[0]["no_trade_reason"] == "BLOCKED_CONFIDENCE"


def test_an_attempted_buy_with_no_fill_counts_as_failed():
    trade_results = [{
        "ticker": "TSM", "cycle_id": "c2",
        "action": "BUY", "policy_action": "EXECUTE_BUY", "confidence": 80,
    }]

    with patch("app.services.pipeline_service.mongo_store") as ms:
        ms.find_docs.side_effect = _store(trade_results, [])
        recovered = recover_ticker_results_from_store("c2")

    s = summarize_ticker_results(recovered)
    assert s["trade_attempted"] == 1
    assert s["trade_executed"] == 0
    assert s["trade_failed"] == 1


def test_a_cycle_with_no_decisions_recovers_an_empty_list_not_an_error():
    with patch("app.services.pipeline_service.mongo_store") as ms:
        ms.find_docs.side_effect = _store([], [])
        assert recover_ticker_results_from_store("c3") == []


def test_recovery_never_raises_into_the_shutdown_path():
    """This runs inside a CancelledError handler. A store failure there must
    not replace the cancellation with a different exception."""
    with patch("app.services.pipeline_service.mongo_store") as ms:
        ms.find_docs.side_effect = RuntimeError("mongo is gone")
        assert recover_ticker_results_from_store("c4") == []
